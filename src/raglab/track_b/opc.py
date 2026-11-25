"""Read bounded, passive workbook parts from local ZIP packages."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path, PurePosixPath
from struct import Struct, error as StructError
from typing import BinaryIO
from weakref import finalize
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo
import zlib

from raglab.offline.windows_paths import WindowsLocalPaths

from .errors import ForbiddenPart, InvalidPackage, PackageLimitExceeded, UnsafeRelationship

REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CONTENT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
CONTENT_TYPES = "[Content_Types].xml"
REQUIRED_PARTS = {CONTENT_TYPES, "xl/workbook.xml"}
FORBIDDEN_DIRECTORIES = {
    "activex", "embeddings", "externallinks", "macrosheets", "querytables",
}
FORBIDDEN_FILENAMES = {"connections.xml", "oleobject.bin", "vbaproject.bin"}
FORBIDDEN_TYPES = (
    "activex", "connections", "ddelink", "externallink", "macroenabled",
    "macrosheet", "oleobject", "querytable", "vbaproject",
)
ZIP_END = Struct("<4s4H2IH")
ZIP_CENTRAL_HEADER = Struct("<4s24x3H12x")
ZIP_ERRORS = (BadZipFile, OSError, RuntimeError, ValueError, EOFError, StructError, zlib.error)


@dataclass(frozen=True)
class OpcLimits:
    max_entries: int = 4096
    max_entry_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_ratio: float = 100.0
    max_xml_depth: int = 64
    max_xml_elements: int = 1_000_000
    max_xml_attributes: int = 2_000_000
    max_xml_attribute_chars: int = 24 * 1024 * 1024
    max_xml_text_chars: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        for limit in (
            self.max_entries, self.max_entry_bytes, self.max_total_bytes,
            self.max_xml_depth, self.max_xml_elements, self.max_xml_attributes,
            self.max_xml_attribute_chars, self.max_xml_text_chars,
        ):
            if type(limit) is not int or limit < 0:
                raise ValueError("ZIP and XML limits must be nonnegative integers")
        if not math.isfinite(self.max_ratio) or self.max_ratio <= 0:
            raise ValueError("ZIP compression ratio limit must be positive and finite")


@dataclass(frozen=True)
class Relationship:
    relationship_id: str
    relationship_type: str
    target: str


class _PassiveTreeBuilder(ET.TreeBuilder):
    def __init__(self, limits: OpcLimits) -> None:
        super().__init__()
        self._limits = limits
        self._depth = 0
        self._elements = 0
        self._attributes = 0
        self._attribute_chars = 0
        self._text_chars = 0

    def _count_attributes(self, count: int, characters: int) -> None:
        self._attributes += count
        self._attribute_chars += characters
        if self._attributes > self._limits.max_xml_attributes:
            raise PackageLimitExceeded("XML attribute count exceeds limit")
        if self._attribute_chars > self._limits.max_xml_attribute_chars:
            raise PackageLimitExceeded("XML attribute text exceeds limit")

    def start_ns(self, prefix: str, uri: str) -> None:
        self._count_attributes(1, len(prefix) + len(uri))

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        self._depth += 1
        self._elements += 1
        if self._depth > self._limits.max_xml_depth:
            raise PackageLimitExceeded("XML depth exceeds limit")
        if self._elements > self._limits.max_xml_elements:
            raise PackageLimitExceeded("XML element count exceeds limit")
        self._count_attributes(len(attrs), sum(len(key) + len(value) for key, value in attrs.items()))
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        self._depth -= 1
        return super().end(tag)

    def data(self, data: str) -> None:
        self._text_chars += len(data)
        if self._text_chars > self._limits.max_xml_text_chars:
            raise PackageLimitExceeded("XML text exceeds limit")
        super().data(data)

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise InvalidPackage("invalid XML: document type declarations are forbidden")


def _file_stamp(stream: BinaryIO) -> tuple[int, ...]:
    info = os.fstat(stream.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _validate_central_directory(stream: BinaryIO, size: int, limits: OpcLimits) -> None:
    # ZipFile loads the entire central directory before it exposes member metadata.
    # Bound its size and count before invoking it.
    tail_size = min(size, ZIP_END.size + 65535)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < ZIP_END.size:
        raise InvalidPackage("invalid ZIP end record")
    _, disk, central_disk, disk_entries, entries, length, start, comment = (
        ZIP_END.unpack_from(tail, position)
    )
    end = size - tail_size + position
    if (
        disk or central_disk or disk_entries != entries
        or entries == 65535 or start + length != end
        or len(tail) - position != ZIP_END.size + comment
    ):
        raise InvalidPackage("unsupported or malformed ZIP layout")
    if entries > limits.max_entries:
        raise PackageLimitExceeded("entry count exceeds limit")
    if length > entries * 4096:
        raise PackageLimitExceeded("central directory metadata exceeds limit")

    for _ in range(entries):
        if start + ZIP_CENTRAL_HEADER.size > end:
            raise InvalidPackage("invalid ZIP central directory")
        stream.seek(start)
        signature, name_size, extra_size, comment_size = ZIP_CENTRAL_HEADER.unpack(
            stream.read(ZIP_CENTRAL_HEADER.size)
        )
        if signature != b"PK\x01\x02":
            raise InvalidPackage("invalid ZIP central directory")
        start += ZIP_CENTRAL_HEADER.size + name_size + extra_size + comment_size
    if start != end:
        raise InvalidPackage("invalid ZIP central directory count")


def _validate_entry_limits(info: ZipInfo, limits: OpcLimits) -> None:
    if info.flag_bits & 1:
        raise InvalidPackage("encrypted ZIP entries are unsupported")
    if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
        raise InvalidPackage("unsupported ZIP compression")
    if info.file_size > limits.max_entry_bytes:
        raise PackageLimitExceeded("entry exceeds uncompressed size limit")
    # Bound compressed input too, with headroom for DEFLATE framing and overhead.
    compressed_limit = limits.max_entry_bytes + limits.max_entry_bytes // 8 + 65536
    if info.compress_size > compressed_limit:
        raise PackageLimitExceeded("compressed entry size exceeds limit")
    if info.file_size / max(info.compress_size, 1) > limits.max_ratio:
        raise PackageLimitExceeded("compression ratio exceeds limit")


def _has_unsafe_path_character(name: str) -> bool:
    return any(character in "\\:%?#" or ord(character) < 32 for character in name)


def _valid_part_name(name: str) -> bool:
    return not _has_unsafe_path_character(name) and all(
        part and part not in {".", ".."} for part in name.split("/")
    )


def _forbidden_part(name: str) -> bool:
    *directories, filename = name.lower().split("/")
    return filename in FORBIDDEN_FILENAMES or bool(FORBIDDEN_DIRECTORIES.intersection(directories))


def _forbidden_type(value: str) -> bool:
    return any(token in value.lower() for token in FORBIDDEN_TYPES)


def _resolve_target(source: PurePosixPath, target: str) -> str:
    if not target or target != target.strip() or _has_unsafe_path_character(target):
        raise UnsafeRelationship("invalid relationship target")
    if target.startswith("/"):
        candidate = target[1:]
        if not _valid_part_name(candidate):
            raise UnsafeRelationship("invalid package-absolute relationship target")
        return candidate
    parts = list(source.parent.parts)
    for part in target.split("/"):
        if not part:
            raise UnsafeRelationship("invalid relationship target")
        if part == "..":
            if not parts:
                raise UnsafeRelationship("relationship escapes package root")
            parts.pop()
        elif part != ".":
            parts.append(part)
    if not parts:
        raise UnsafeRelationship("invalid relationship target")
    return "/".join(parts)


class OpcPackage:
    """Read a workbook while its validated Windows file handle remains pinned."""

    def __init__(
        self, path: Path, limits: OpcLimits, names: frozenset[str],
        stamp: tuple[int, ...], stream: BinaryIO, archive: ZipFile, resources: ExitStack,
    ) -> None:
        self.path = path
        self.limits = limits
        self.names = names
        self._stamp = stamp
        self._stream = stream
        self._archive = archive
        self._close = finalize(self, resources.close)

    @classmethod
    def open(cls, path: Path, limits: OpcLimits = OpcLimits()) -> OpcPackage:
        resources = ExitStack()
        try:
            paths = WindowsLocalPaths()
            try:
                local = paths.validate(path)
                pinned = resources.enter_context(paths.pin(local, reject_aliases=True))
            except (OSError, ValueError):
                raise InvalidPackage("input must be a regular local file") from None
            stream = pinned.stream
            if stream is None:
                raise InvalidPackage("input must be a regular local file")
            try:
                stamp = _file_stamp(stream)
                _validate_central_directory(stream, stamp[2], limits)
                archive = resources.enter_context(ZipFile(stream))
                names = cls._validate_infos(archive.infolist(), limits)
                if _file_stamp(stream) != stamp:
                    raise InvalidPackage("package changed during validation")
            except ZIP_ERRORS:
                raise InvalidPackage("input is not a valid OPC ZIP package") from None
            if not REQUIRED_PARTS <= names:
                raise InvalidPackage("required workbook parts are missing")
            package = cls(path, limits, names, stamp, stream, archive, resources)
            package._validate_content_types()
        except BaseException:
            resources.close()
            raise
        return package

    def __enter__(self) -> OpcPackage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._close()

    @staticmethod
    def _validate_infos(infos: list[ZipInfo], limits: OpcLimits) -> frozenset[str]:
        seen: set[str] = set()
        names: set[str] = set()
        total = 0
        for info in infos:
            # ZipInfo may trim or normalize the filename when it constructs .filename.
            name = info.orig_filename
            if not _valid_part_name(name) or name != info.filename:
                raise InvalidPackage("unsafe part name")
            canonical = name.casefold()
            if canonical in seen:
                raise InvalidPackage("duplicate part name")
            seen.add(canonical)
            names.add(name)
            if _forbidden_part(name):
                raise ForbiddenPart("forbidden workbook part")
            _validate_entry_limits(info, limits)
            total += info.file_size
        if total > limits.max_total_bytes:
            raise PackageLimitExceeded("total uncompressed size exceeds limit")
        return frozenset(names)

    def _validate_content_types(self) -> None:
        types = self.read_xml(CONTENT_TYPES)
        if types.tag != f"{CONTENT_NS}Types":
            raise InvalidPackage("invalid content types part")
        for node in types:
            if node.tag not in {f"{CONTENT_NS}Default", f"{CONTENT_NS}Override"}:
                raise InvalidPackage("invalid content types part")
            if _forbidden_type(node.get("ContentType", "")):
                raise ForbiddenPart("forbidden workbook content type")

    def _require_open(self) -> None:
        if not self._close.alive:
            raise InvalidPackage("package is closed")

    @contextmanager
    def _member(self, part_name: str):
        self._require_open()
        if part_name not in self.names:
            raise InvalidPackage("missing part")
        try:
            if _file_stamp(self._stream) != self._stamp:
                raise InvalidPackage("package changed after validation")
            info = self._archive.getinfo(part_name)
            with self._archive.open(info) as part:
                yield part, info.file_size
            if _file_stamp(self._stream) != self._stamp:
                raise InvalidPackage("package changed during read")
        except (*ZIP_ERRORS, KeyError):
            raise InvalidPackage("unable to read ZIP part") from None

    def read_bytes(self, part_name: str) -> bytes:
        with self._member(part_name) as (part, size):
            data = part.read(size + 1)
            if len(data) != size:
                raise InvalidPackage("inconsistent ZIP entry size")
            return data

    def read_xml(self, part_name: str) -> ET.Element:
        parser = ET.XMLParser(target=_PassiveTreeBuilder(self.limits))
        with self._member(part_name) as (part, size):
            try:
                consumed = 0
                while chunk := part.read(64 * 1024):
                    consumed += len(chunk)
                    parser.feed(chunk)
                if consumed != size:
                    raise InvalidPackage("inconsistent ZIP entry size")
                return parser.close()
            except (ET.ParseError, LookupError, ValueError):
                raise InvalidPackage("invalid XML part") from None

    def relationships(self, source_part: str) -> tuple[Relationship, ...]:
        self._require_open()
        if source_part not in self.names:
            raise InvalidPackage("missing relationship source")
        source = PurePosixPath(source_part)
        rels_name = str(source.parent / "_rels" / f"{source.name}.rels")
        if rels_name not in self.names:
            return ()
        root = self.read_xml(rels_name)
        if root.tag != f"{REL_NS}Relationships":
            raise UnsafeRelationship("invalid relationships part")
        output: list[Relationship] = []
        ids: set[str] = set()
        for node in root:
            if node.tag != f"{REL_NS}Relationship":
                raise UnsafeRelationship("invalid relationship entry")
            if node.get("TargetMode", "Internal") != "Internal":
                raise UnsafeRelationship("External or unsupported relationship mode")
            relationship_id = node.get("Id", "")
            relationship_type = node.get("Type", "")
            if not relationship_id or relationship_id in ids or not relationship_type:
                raise UnsafeRelationship("invalid relationship metadata")
            if _forbidden_type(relationship_type):
                raise ForbiddenPart("forbidden workbook relationship type")
            target = _resolve_target(source, node.get("Target", ""))
            if target not in self.names:
                raise UnsafeRelationship("missing relationship target")
            ids.add(relationship_id)
            output.append(Relationship(relationship_id, relationship_type, target))
        return tuple(output)

import ctypes
import ntpath
import os
import re
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as win
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PureWindowsPath
from typing import BinaryIO


_SYSTEM32_ONLY = 0x00000800
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_CREATE_NEW = 1
_FILE_LIST_DIRECTORY = 1
_OVERLAPPED_IO = 0x40000000
_NAMESPACE_CHANGES = 0x00000007  # File names, directory names, and attributes
_WAIT_TIMEOUT = 0x00000102
_FILE_RENAME_INFO = 3
_FILE_DISPOSITION_INFO = 4
_OPEN_EXISTING = 3
_OPEN_REPARSE_POINT = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_ATTRIBUTE_DIRECTORY = 0x10
_ATTRIBUTE_REPARSE_POINT = 0x400
_LOCAL_DRIVE_TYPES = {2, 3, 5, 6}
_NATIVE_VOLUME = re.compile(r"\\Device\\(?:HarddiskVolume|CdRom|Floppy|Ramdisk)[0-9]+", re.I)


def system_dll(name: str):
    if os.name != "nt":
        raise OSError("Windows system API unavailable")
    return ctypes.WinDLL(name, winmode=_SYSTEM32_ONLY, use_last_error=True)


def bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = arguments
    return function


def path_segment_is_safe(part: str) -> bool:
    return bool(part) and len(part) <= 255 and not (
        part.endswith((" ", "."))
        or PureWindowsPath(part).is_reserved()
        or any(char in '<>:"|?*/\\' or ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in part)
    )


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_void_p), ("high", ctypes.c_void_p),
        ("offset", win.DWORD), ("offset_high", win.DWORD), ("event", win.HANDLE),
    ]


class _FileRenameInformation(ctypes.Structure):
    _fields_ = [
        ("replace", win.DWORD), ("directory", win.HANDLE),
        ("name_length", win.DWORD), ("name", win.WCHAR * 1),
    ]


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", win.DWORD),
        ("created", win.FILETIME),
        ("accessed", win.FILETIME),
        ("updated", win.FILETIME),
        ("volume", win.DWORD),
        ("size_high", win.DWORD),
        ("size_low", win.DWORD),
        ("link_count", win.DWORD),
        ("index_high", win.DWORD),
        ("index_low", win.DWORD),
    ]


class _FileIdInformation(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_uint64), ("index", ctypes.c_ubyte * 16)]


@dataclass(frozen=True)
class FileIdentity:
    volume: int
    index: bytes
    size: int
    updated: int
    is_directory: bool

    @property
    def file_id(self) -> tuple[int, bytes]:
        return self.volume, self.index


@dataclass(frozen=True)
class PinnedPath:
    path: Path
    info: FileIdentity | None
    handle: int | None = None
    stream: BinaryIO | None = None

    def read_bytes(self, maximum: int) -> bytes:
        if self.stream is None or self.info is None or self.info.size > maximum:
            raise ValueError("file read exceeds limit or is unavailable")
        self.stream.seek(0)
        data = self.stream.read(self.info.size + 1)
        if len(data) != self.info.size:
            raise ValueError("file size changed")
        return data


class WindowsLocalPaths:
    @cached_property
    def _kernel(self):
        return system_dll("kernel32.dll")

    def drive_type(self, anchor: str) -> int:
        drive_type = bind(self._kernel, "GetDriveTypeW", win.UINT, win.LPCWSTR)
        return drive_type(anchor)

    def volume_target(self, drive: str) -> str:
        query = bind(self._kernel, "QueryDosDeviceW", win.DWORD, win.LPCWSTR, win.LPWSTR, win.DWORD)
        buffer = ctypes.create_unicode_buffer(32768)
        if not query(drive, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def validate(self, value: str | Path) -> Path:
        # Validate the caller spelling before any access to a filesystem path.
        text = os.fspath(value).replace("/", "\\")
        if len(text) > 32760 or text.startswith("\\\\"):
            raise ValueError("UNC and device paths are unavailable")
        drive, tail = ntpath.splitdrive(text)
        if drive and (not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\")):
            raise ValueError("drive-relative or special paths are unavailable")
        if any(
            not path_segment_is_safe(segment)
            for segment in tail.split("\\")
            if segment not in {"", ".", ".."}
        ):
            raise ValueError("invalid local path")
        if os.name != "nt":
            raise ValueError("Windows local path verification is unavailable")
        absolute = Path(ntpath.abspath(text))
        if not re.fullmatch(r"[A-Za-z]:\\", absolute.anchor):
            raise ValueError("expected a local drive")
        if len(absolute.parts) > 256 or not _NATIVE_VOLUME.fullmatch(self.volume_target(absolute.drive)):
            raise ValueError("expected a direct local volume")
        if self.drive_type(absolute.anchor) not in _LOCAL_DRIVE_TYPES:
            raise ValueError("expected a local drive")
        return absolute

    def identity(self, handle: int) -> FileIdentity:
        query = bind(
            self._kernel, "GetFileInformationByHandle", win.BOOL,
            win.HANDLE, ctypes.POINTER(_FileInformation),
        )
        data = _FileInformation()
        if not query(handle, ctypes.byref(data)):
            raise ctypes.WinError(ctypes.get_last_error())
        if data.attributes & _ATTRIBUTE_REPARSE_POINT:
            raise ValueError("reparse points are unavailable")
        query_id = bind(
            self._kernel, "GetFileInformationByHandleEx", win.BOOL,
            win.HANDLE, ctypes.c_int, ctypes.c_void_p, win.DWORD,
        )
        file_id = _FileIdInformation()
        if not query_id(handle, 18, ctypes.byref(file_id), ctypes.sizeof(file_id)):  # FileIdInfo
            raise ctypes.WinError(ctypes.get_last_error())
        return FileIdentity(
            int(file_id.volume),
            bytes(file_id.index),
            data.size_high << 32 | data.size_low,
            data.updated.dwHighDateTime << 32 | data.updated.dwLowDateTime,
            bool(data.attributes & _ATTRIBUTE_DIRECTORY),
        )

    def _canonical(self, handle: int) -> Path:
        final_path = bind(
            self._kernel, "GetFinalPathNameByHandleW", win.DWORD,
            win.HANDLE, win.LPWSTR, win.DWORD, win.DWORD,
        )
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ValueError("canonical path unavailable")
        path = buffer.value
        if not re.match(r"^\\\\\?\\[A-Za-z]:\\", path):
            raise ValueError("expected a local canonical path")
        return Path(path[4:])

    @contextmanager
    def pin_entry(self, path: Path, *, directory_write_sharing: bool = False):
        # The caller holds and has checked every ancestor. Open just this
        # component without following a reparse point and deny writes/deletes.
        import msvcrt

        create_file = bind(
            self._kernel, "CreateFileW", win.HANDLE,
            win.LPCWSTR, win.DWORD, win.DWORD, ctypes.c_void_p,
            win.DWORD, win.DWORD, win.HANDLE,
        )
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        sharing = _FILE_SHARE_READ | (_FILE_SHARE_WRITE if directory_write_sharing else 0)
        handle = create_file(
            "\\\\?\\" + str(path), _GENERIC_READ, sharing, None,
            _OPEN_EXISTING, _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS, None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        owns_handle = True
        try:
            info = self.identity(handle)
            canonical = self._canonical(handle)
            if directory_write_sharing and not info.is_directory:
                # Reopen regular files with exclusive read-sharing.
                close(handle)
                owns_handle = False
                with self.pin_entry(path) as strict:
                    yield strict
            elif info.is_directory:
                yield PinnedPath(canonical, info, handle)
            else:
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                owns_handle = False
                try:
                    stream = os.fdopen(descriptor, "rb", buffering=0)
                except BaseException:
                    os.close(descriptor)
                    raise
                with stream:
                    yield PinnedPath(canonical, info, handle, stream)
        finally:
            if owns_handle:
                close(handle)

    @contextmanager
    def pin(
        self, path: Path, *, missing_ok: bool = False, create_directories: bool = False,
        directory_write_sharing: bool = False,
    ):
        # Validate before calling this method. Pin one segment at a time so
        # opening a child cannot traverse an unverified ancestor.
        with ExitStack() as locks:
            root = locks.enter_context(self.pin_entry(Path(path.anchor), directory_write_sharing=directory_write_sharing))
            entry = root
            for index, name in enumerate(path.parts[1:], start=1):
                if not entry.info.is_directory:
                    raise ValueError("a path ancestor is not a directory")
                child = entry.path / name
                try:
                    entry = locks.enter_context(self.pin_entry(child, directory_write_sharing=directory_write_sharing))
                except FileNotFoundError:
                    if create_directories:
                        child.mkdir(exist_ok=True)
                        entry = locks.enter_context(self.pin_entry(child, directory_write_sharing=directory_write_sharing))
                    elif missing_ok:
                        yield PinnedPath(entry.path.joinpath(*path.parts[index:]), None)
                        return
                    else:
                        raise
            if create_directories and not entry.info.is_directory:
                raise ValueError("report parent is not a directory")
            yield entry


    @contextmanager
    def watch_namespace(self, directory: PinnedPath):
        # One outstanding recursive Windows notification stays armed from intake
        # through the final decision. Any completion, including overflow, blocks.
        create_file = bind(
            self._kernel, "CreateFileW", win.HANDLE, win.LPCWSTR, win.DWORD,
            win.DWORD, ctypes.c_void_p, win.DWORD, win.DWORD, win.HANDLE,
        )
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        create_event = bind(
            self._kernel, "CreateEventW", win.HANDLE, ctypes.c_void_p, win.BOOL, win.BOOL, win.LPCWSTR,
        )
        watch = bind(
            self._kernel, "ReadDirectoryChangesW", win.BOOL, win.HANDLE,
            ctypes.c_void_p, win.DWORD, win.BOOL, win.DWORD,
            ctypes.c_void_p, ctypes.POINTER(_Overlapped), ctypes.c_void_p,
        )
        wait = bind(self._kernel, "WaitForSingleObject", win.DWORD, win.HANDLE, win.DWORD)
        cancel = bind(
            self._kernel, "CancelIoEx", win.BOOL, win.HANDLE, ctypes.POINTER(_Overlapped),
        )
        completion = bind(
            self._kernel, "GetOverlappedResult", win.BOOL, win.HANDLE, ctypes.POINTER(_Overlapped),
            ctypes.POINTER(win.DWORD), win.BOOL,
        )
        event = create_event(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        handle = None
        try:
            handle = create_file(
                "\\\\?\\" + str(directory.path), _FILE_LIST_DIRECTORY, _FILE_SHARE_READ, None,
                _OPEN_EXISTING, _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS | _OVERLAPPED_IO, None,
            )
            if handle == _INVALID_HANDLE:
                raise ctypes.WinError(ctypes.get_last_error())
            if self.identity(handle).file_id != directory.info.file_id:
                raise ValueError("bundle directory changed")
            operation = _Overlapped(event=event)
            buffer = ctypes.create_string_buffer(4096)
            if not watch(handle, buffer, len(buffer), True, _NAMESPACE_CHANGES, None, ctypes.byref(operation), None):
                raise ctypes.WinError(ctypes.get_last_error())

            def require_unchanged() -> None:
                if wait(event, 0) != _WAIT_TIMEOUT:
                    raise ValueError("bundle namespace changed or notification unavailable")

            try:
                yield require_unchanged
            finally:
                # Keep the I/O buffer and OVERLAPPED alive until completion even
                # when cancellation races with a filesystem notification.
                cancel(handle, ctypes.byref(operation))
                transferred = win.DWORD()
                completion(handle, ctypes.byref(operation), ctypes.byref(transferred), True)
        finally:
            if handle not in {None, _INVALID_HANDLE}:
                close(handle)
            close(event)

    def _rename_file(self, handle: int, destination: Path) -> None:
        set_information = bind(
            self._kernel, "SetFileInformationByHandle", win.BOOL, win.HANDLE,
            ctypes.c_int, ctypes.c_void_p, win.DWORD,
        )
        name = ("\\\\?\\" + str(destination)).encode("utf-16-le")
        offset = _FileRenameInformation.name.offset
        # Win32 reads a terminating WCHAR in addition to the counted name.
        buffer = ctypes.create_string_buffer(max(ctypes.sizeof(_FileRenameInformation), offset + len(name) + 2))
        header = _FileRenameInformation.from_buffer(buffer)
        header.replace = 1
        header.name_length = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + offset, name, len(name))
        if not set_information(handle, _FILE_RENAME_INFO, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())

    def publish_report(
        self, parent: PinnedPath, destination: Path, content: bytes, release_protection: Callable[[], None],
    ) -> None:
        create_file = bind(
            self._kernel, "CreateFileW", win.HANDLE, win.LPCWSTR, win.DWORD,
            win.DWORD, ctypes.c_void_p, win.DWORD, win.DWORD, win.HANDLE,
        )
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        flush = bind(self._kernel, "FlushFileBuffers", win.BOOL, win.HANDLE)
        write = bind(
            self._kernel, "WriteFile", win.BOOL, win.HANDLE, ctypes.c_void_p, win.DWORD,
            ctypes.POINTER(win.DWORD), ctypes.c_void_p,
        )
        set_information = bind(
            self._kernel, "SetFileInformationByHandle", win.BOOL, win.HANDLE,
            ctypes.c_int, ctypes.c_void_p, win.DWORD,
        )
        staged = parent.path / f".raglab-{uuid.uuid4().hex}.tmp"
        handle = create_file(
            "\\\\?\\" + str(staged), _GENERIC_READ | _GENERIC_WRITE | _DELETE,
            0, None, _CREATE_NEW, _OPEN_REPARSE_POINT, None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        published = False
        try:
            written = win.DWORD()
            if not write(handle, content, len(content), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value != len(content) or not flush(handle):
                raise OSError("report write or flush failed")
            with self.pin(parent.path, directory_write_sharing=True) as publishing:
                if publishing.info.file_id != parent.info.file_id:
                    raise ValueError("report parent changed")
                release_protection()
                self._rename_file(handle, destination)
                published = True
                if self.identity(publishing.handle).file_id != parent.info.file_id:
                    raise ValueError("report parent changed")
        finally:
            if not published:
                delete = ctypes.c_ubyte(1)
                set_information(handle, _FILE_DISPOSITION_INFO, ctypes.byref(delete), ctypes.sizeof(delete))
            close(handle)

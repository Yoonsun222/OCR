import ctypes
import ntpath
import os
import re
import uuid
from collections.abc import Callable, Mapping
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
_FILE_SHARE_DELETE = 0x00000004
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_CREATE_NEW = 1
_NT_FILE_CREATE = 2
_NT_FILE_DIRECTORY_FILE = 0x00000001
_NT_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_OBJ_CASE_INSENSITIVE = 0x00000040
_OBJ_DONT_REPARSE = 0x00001000
_SYNCHRONIZE = 0x00100000
_FILE_LIST_DIRECTORY = 1
_OVERLAPPED_IO = 0x40000000
_NAMESPACE_CHANGES = 0x00000007  # File names, directory names, and attributes
_WAIT_TIMEOUT = 0x00000102
_FILE_RENAME_INFO = 3
_FILE_DISPOSITION_INFO = 4
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
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


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", win.USHORT), ("maximum_length", win.USHORT), ("buffer", win.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", win.ULONG),
        ("root_directory", win.HANDLE),
        ("object_name", ctypes.POINTER(_UnicodeString)),
        ("attributes", win.ULONG),
        ("security_descriptor", ctypes.c_void_p),
        ("security_quality_of_service", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    # The first field is a native union of NTSTATUS and PVOID.
    _fields_ = [("status_or_pointer", ctypes.c_void_p), ("information", ctypes.c_size_t)]


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
    link_count: int = 1

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
            int(data.link_count),
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
    def open_progress_stream(self, path: Path):
        """Hold the validated namespace and give one writer exclusive access."""
        path = self.validate(path)
        import msvcrt

        from raglab.offline.windows_names import windows_name_compare

        create_file = bind(
            self._kernel, "CreateFileW", win.HANDLE,
            win.LPCWSTR, win.DWORD, win.DWORD, ctypes.c_void_p,
            win.DWORD, win.DWORD, win.HANDLE,
        )
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        with self.pin(path.parent, create_directories=True, reject_aliases=True) as parent:
            destination = parent.path / path.name
            handle = create_file(
                "\\\\?\\" + str(destination), _GENERIC_READ | _GENERIC_WRITE,
                0, None, _OPEN_ALWAYS, _OPEN_REPARSE_POINT, None,
            )
            if handle == _INVALID_HANDLE:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                info = self.identity(handle)
                if info.is_directory or info.link_count != 1 or windows_name_compare(
                    str(destination), str(self._canonical(handle)),
                ) != 0:
                    raise ValueError("aliased progress file is unavailable")
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except BaseException:
                close(handle)
                raise
            try:
                stream = os.fdopen(descriptor, "r+b", buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                yield stream

    @contextmanager
    def pin(
        self, path: Path, *, missing_ok: bool = False, create_directories: bool = False,
        directory_write_sharing: bool = False, reject_aliases: bool = False,
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
                if reject_aliases:
                    from raglab.offline.windows_names import windows_name_compare

                    if windows_name_compare(str(entry.path), str(child)) != 0:
                        raise ValueError("aliased path component is unavailable")
            if create_directories and not entry.info.is_directory:
                raise ValueError("report parent is not a directory")
            yield entry


    @contextmanager
    def watch_namespace(self, directory: PinnedPath, *, owned_for_rename: bool = False):
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
                "\\\\?\\" + str(directory.path), _FILE_LIST_DIRECTORY,
                _FILE_SHARE_READ | (_FILE_SHARE_DELETE if owned_for_rename else 0), None,
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

    def _rename_file(self, handle: int, destination: Path, *, replace: bool = True) -> None:
        set_information = bind(
            self._kernel, "SetFileInformationByHandle", win.BOOL, win.HANDLE,
            ctypes.c_int, ctypes.c_void_p, win.DWORD,
        )
        name = ("\\\\?\\" + str(destination)).encode("utf-16-le")
        offset = _FileRenameInformation.name.offset
        # Win32 reads a terminating WCHAR in addition to the counted name.
        buffer = ctypes.create_string_buffer(max(ctypes.sizeof(_FileRenameInformation), offset + len(name) + 2))
        header = _FileRenameInformation.from_buffer(buffer)
        header.replace = replace
        header.name_length = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + offset, name, len(name))
        if not set_information(handle, _FILE_RENAME_INFO, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())

    def _create_owned_file(self, path: Path) -> int:
        create_file = bind(
            self._kernel, "CreateFileW", win.HANDLE, win.LPCWSTR, win.DWORD,
            win.DWORD, ctypes.c_void_p, win.DWORD, win.DWORD, win.HANDLE,
        )
        handle = create_file(
            "\\\\?\\" + str(path), _GENERIC_READ | _GENERIC_WRITE | _DELETE,
            0, None, _CREATE_NEW, _OPEN_REPARSE_POINT, None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _write_owned_file(self, handle: int, content: bytes) -> None:
        flush = bind(self._kernel, "FlushFileBuffers", win.BOOL, win.HANDLE)
        write = bind(
            self._kernel, "WriteFile", win.BOOL, win.HANDLE, ctypes.c_void_p, win.DWORD,
            ctypes.POINTER(win.DWORD), ctypes.c_void_p,
        )
        written = win.DWORD()
        if not write(handle, content, len(content), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(content) or not flush(handle):
            raise OSError("report write or flush failed")

    def _discard_owned_file(self, handle: int) -> None:
        set_information = bind(
            self._kernel, "SetFileInformationByHandle", win.BOOL, win.HANDLE,
            ctypes.c_int, ctypes.c_void_p, win.DWORD,
        )
        delete = ctypes.c_ubyte(1)
        if not set_information(handle, _FILE_DISPOSITION_INFO, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())

    def probe_file(
        self, parent: PinnedPath, name: str, content: bytes,
        *, staged_name: str | None = None, release_protection: Callable[[], None],
    ) -> None:
        # The caller holds parent and every ancestor with delete sharing denied.
        # Exclusively create and remove by the same handle, even after a rename.
        if not path_segment_is_safe(name) or (staged_name is not None and not path_segment_is_safe(staged_name)):
            raise ValueError("invalid probe file name")
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        seek = bind(self._kernel, "SetFilePointerEx", win.BOOL, win.HANDLE, ctypes.c_longlong, ctypes.c_void_p, win.DWORD)
        read = bind(
            self._kernel, "ReadFile", win.BOOL, win.HANDLE, ctypes.c_void_p,
            win.DWORD, ctypes.POINTER(win.DWORD), ctypes.c_void_p,
        )
        destination = parent.path / name
        handle = self._create_owned_file(parent.path / (staged_name if staged_name is not None else name))
        try:
            self._write_owned_file(handle, content)
            if staged_name is not None:
                # Rename requires write sharing on the directory. The staged
                # file already keeps it nonempty and the new pin denies deletion.
                with self.pin(parent.path, directory_write_sharing=True) as publishing:
                    if publishing.info.file_id != parent.info.file_id:
                        raise ValueError("probe parent changed")
                    release_protection()
                    self._rename_file(handle, destination, replace=False)
                    if self.identity(publishing.handle).file_id != parent.info.file_id:
                        raise ValueError("probe parent changed")
            if str(self._canonical(handle)) != str(destination):
                raise ValueError("probe file path did not round-trip")
            actual = ctypes.create_string_buffer(len(content) + 1)
            count = win.DWORD()
            if not seek(handle, 0, None, 0) or not read(handle, actual, len(actual), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if actual.raw[:count.value] != content:
                raise OSError("probe file read-back did not match")
        finally:
            try:
                self._discard_owned_file(handle)
            finally:
                if not close(handle):
                    raise ctypes.WinError(ctypes.get_last_error())

    def _create_owned_directory(self, parent: PinnedPath, name: str) -> int:
        """Create exactly one child and acquire its delete-capable handle atomically."""
        if parent.handle is None or not path_segment_is_safe(name):
            raise ValueError("invalid directory creation operand")
        ntdll = system_dll("ntdll.dll")
        create_file = bind(
            ntdll, "NtCreateFile", win.LONG,
            ctypes.POINTER(win.HANDLE), win.DWORD, ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p, win.ULONG, win.ULONG,
            win.ULONG, win.ULONG, ctypes.c_void_p, win.ULONG,
        )
        status_to_error = bind(ntdll, "RtlNtStatusToDosError", win.ULONG, win.LONG)
        buffer = ctypes.create_unicode_buffer(name)
        length = ctypes.sizeof(buffer) - ctypes.sizeof(win.WCHAR)
        relative_name = _UnicodeString(length, ctypes.sizeof(buffer), ctypes.cast(buffer, win.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), parent.handle, ctypes.pointer(relative_name),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE, None, None,
        )
        handle = win.HANDLE()
        io_status = _IoStatusBlock()
        status = create_file(
            ctypes.byref(handle), _GENERIC_READ | _DELETE | _SYNCHRONIZE,
            ctypes.byref(attributes), ctypes.byref(io_status), None, 0, _FILE_SHARE_READ,
            _NT_FILE_CREATE, _NT_FILE_DIRECTORY_FILE | _NT_FILE_SYNCHRONOUS_IO_NONALERT,
            None, 0,
        )
        if status < 0:
            raise ctypes.WinError(status_to_error(status))
        return handle.value

    def _open_for_removal(self, path: Path) -> int:
        create_file = bind(
            self._kernel, "CreateFileW", win.HANDLE,
            win.LPCWSTR, win.DWORD, win.DWORD, ctypes.c_void_p,
            win.DWORD, win.DWORD, win.HANDLE,
        )
        handle = create_file(
            "\\\\?\\" + str(path), _GENERIC_READ | _DELETE, _FILE_SHARE_READ, None,
            _OPEN_EXISTING, _OPEN_REPARSE_POINT, None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _remove_owned_members(self, directory: Path, members: Mapping[str, tuple[int, bytes]]) -> None:
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        for member, file_id in members.items():
            try:
                handle = self._open_for_removal(directory / member)
            except FileNotFoundError:
                continue
            try:
                info = self.identity(handle)
                if not info.is_directory and info.file_id == file_id:
                    self._discard_owned_file(handle)
            finally:
                close(handle)

    def publish_new_directory(self, parent: PinnedPath, name: str, files: Mapping[str, bytes]) -> None:
        """Stage owned files, then publish the owned directory without replacement."""
        if not parent.info.is_directory or not path_segment_is_safe(name) or any(
            not path_segment_is_safe(member) for member in files
        ):
            raise ValueError("invalid directory publication")
        destination = parent.path / name
        try:
            with self.pin_entry(destination):
                raise FileExistsError("destination already exists")
        except FileNotFoundError:
            pass

        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        staging_name = f".raglab-{uuid.uuid4().hex}.tmp"
        staging = parent.path / staging_name
        handle = self._create_owned_directory(parent, staging_name)
        owned = {}
        owned_directory = False
        published = False
        with ExitStack() as checked:
            try:
                info = self.identity(handle)
                if not info.is_directory or str(self._canonical(handle)) != str(staging):
                    raise ValueError("unexpected staging directory")
                owned_directory = True
                directory = PinnedPath(staging, info, handle)
                with ExitStack() as entries, ExitStack() as rollback:
                    for member, content in files.items():
                        file = self._create_owned_file(staging / member)
                        entries.callback(close, file)
                        rollback.callback(self._discard_owned_file, file)
                        owned[member] = self.identity(file).file_id
                        self._write_owned_file(file, content)
                    with self.watch_namespace(directory, owned_for_rename=True) as unchanged:
                        with os.scandir(staging) as contents:
                            names = {entry.name for _, entry in zip(range(len(files) + 1), contents)}
                            if names != files.keys():
                                raise ValueError("unexpected staged inventory")
                        unchanged()
                        # Windows requires closed child handles for directory rename.
                        # Keep the directory and watcher held, then pin and verify
                        # each child again before releasing the owned directory.
                        rollback.pop_all()
                        entries.close()
                        self._rename_file(handle, destination, replace=False)
                        for member, content in files.items():
                            entry = checked.enter_context(self.pin_entry(destination / member))
                            if (
                                entry.info.file_id != owned[member] or entry.info.link_count != 1
                                or entry.path.name != member or entry.read_bytes(len(content)) != content
                            ):
                                raise ValueError("staged file changed")
                        unchanged()
                        published = True
            finally:
                try:
                    if owned_directory and not published:
                        checked.close()
                        current = self._canonical(handle)
                        if current == destination:
                            # Keep a failed directory outside the destination even
                            # when a foreign entry prevents removing it.
                            current = parent.path / f".raglab-{uuid.uuid4().hex}.tmp"
                            self._rename_file(handle, current, replace=False)
                        self._remove_owned_members(current, owned)
                        self._discard_owned_file(handle)
                finally:
                    close(handle)

    def publish_new_files(
        self, parent: PinnedPath, files: Mapping[str, bytes], release_protection: Callable[[], None],
    ) -> None:
        """Publish new files in order, making the last file readable last."""
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        with ExitStack() as handles, ExitStack() as rollback:
            staged = {}
            # ExitStack closes in reverse order. Register the final publication's
            # handle first so it stays exclusive until all prior files are readable.
            for name in reversed(tuple(files)):
                if not path_segment_is_safe(name):
                    raise ValueError("invalid output file name")
                handle = self._create_owned_file(parent.path / f".raglab-{uuid.uuid4().hex}.tmp")
                handles.callback(close, handle)
                rollback.callback(self._discard_owned_file, handle)
                self._write_owned_file(handle, files[name])
                staged[name] = handle
            with self.pin(parent.path, directory_write_sharing=True) as publishing:
                if publishing.info.file_id != parent.info.file_id:
                    raise ValueError("output directory changed")
                release_protection()
                for name in files:
                    self._rename_file(staged[name], publishing.path / name, replace=False)
                rollback.pop_all()

    def publish_report(
        self, parent: PinnedPath, destination: Path, content: bytes, release_protection: Callable[[], None],
    ) -> None:
        close = bind(self._kernel, "CloseHandle", win.BOOL, win.HANDLE)
        staged = parent.path / f".raglab-{uuid.uuid4().hex}.tmp"
        handle = self._create_owned_file(staged)
        published = False
        try:
            self._write_owned_file(handle, content)
            with self.pin(parent.path, directory_write_sharing=True) as publishing:
                if publishing.info.file_id != parent.info.file_id:
                    raise ValueError("report parent changed")
                release_protection()
                self._rename_file(handle, destination)
                published = True
                if self.identity(publishing.handle).file_id != parent.info.file_id:
                    raise ValueError("report parent changed")
        finally:
            try:
                if not published:
                    self._discard_owned_file(handle)
            finally:
                close(handle)

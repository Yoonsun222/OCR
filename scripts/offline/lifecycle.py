"""Standard-user Windows lifecycle, launched only from authenticated toolkit media."""

import argparse
from contextlib import ExitStack, contextmanager, nullcontext
import ctypes
from ctypes import wintypes
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import locale
import msvcrt
import os
from pathlib import Path
from types import SimpleNamespace
import platform
import re
import subprocess
import sys
import time
import uuid
import venv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from raglab.cli import main as raglab
from raglab.offline.bundle import BundleFailure, BundleReport, verify_bundle
from raglab.offline.config import load_profile_config
from raglab.offline.demo import DemoRegion, DemoResult, read_doctor_report
from raglab.offline.jsonio import serialize_json
from raglab.offline.progress import ProgressEvent
from raglab.offline.repro import ApprovedDatasetManifest, ReproInputs, build_repro_bundle
from raglab.offline.windows import WindowsCatalogVerifier, acceptance_host_snapshot
from raglab.offline.windows_paths import PinnedPath, WindowsLocalPaths, bind, path_segment_is_safe, system_dll

PATHS = WindowsLocalPaths()
MARKER = ".raglab-install.json"
DATA_MARKER = ".raglab-data.json"
INSTALL_REPORT = "install-report.json"
HEX = re.compile(r"[0-9a-f]{64}")
PROFILE = ("local16", "local32")
REQUIRED = {
    "raglab.ps1", "src/raglab/__init__.py", "src/raglab/__main__.py",
    "src/raglab/cli.py", "src/raglab/offline/bundle.py", "src/raglab/offline/config.py",
    "src/raglab/offline/demo.py", "src/raglab/offline/doctor.py", "src/raglab/offline/repro.py",
    "src/raglab/offline/windows.py", "configs/security-defaults.json",
    "configs/profiles/local16.json", "configs/profiles/local32.json",
    "fixtures/synthetic-mini/source.svg", "fixtures/synthetic-mini/expected.json",
}
DATA_DIRS = {"tmp", "cache", "runs", "logs"}
DATA_FILES = {DATA_MARKER}
SAFE_INSTALL_RECEIPT = {"schema_version", "status", "action", "generation_fingerprint", "profile"}


class Block(RuntimeError):
    def __init__(self, number=3):
        self.number = number


def native(path):
    return "\\\\?\\" + str(path)


def normalized(path):
    return os.path.normcase(str(path))


def assert_disjoint(*roots):
    for index, first in enumerate(roots):
        for second in roots[:index]:
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise Block(2)


def protected_root(value):
    root = PATHS.validate(value)
    system_roots = tuple(
        Path(value) for name in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
        if (value := os.environ.get(name))
    )
    user_roots = tuple(
        Path(value) for name in ("PUBLIC", "USERPROFILE", "LOCALAPPDATA", "APPDATA")
        if (value := os.environ.get(name))
    )
    protected = (ROOT, Path.cwd(), Path.home(), *user_roots, *system_roots)
    if root == Path(root.anchor) or any(path.is_relative_to(root) for path in protected):
        raise Block(2)
    if any(root.is_relative_to(path) for path in system_roots):
        raise Block(2)
    with PATHS.pin(root.parent, directory_write_sharing=True, reject_aliases=True):
        pass
    return root


def admin_token():
    shell = system_dll("shell32.dll")
    return bool(bind(shell, "IsUserAnAdmin", wintypes.BOOL)())


def local_input(value):
    path = PATHS.validate(value)
    with PATHS.pin(path, directory_write_sharing=True, reject_aliases=True) as held:
        if not held.info.is_directory:
            raise Block(2)
        return held.path


@contextmanager
def serialized(root):
    kernel = system_dll("kernel32.dll")
    close = bind(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    create = bind(kernel, "CreateMutexW", wintypes.HANDLE, ctypes.c_void_p,
                  wintypes.BOOL, wintypes.LPCWSTR)
    wait = bind(kernel, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    release = bind(kernel, "ReleaseMutex", wintypes.BOOL, wintypes.HANDLE)
    key = hashlib.sha256(normalized(root).encode("utf-8")).hexdigest()
    handle = create(None, False, "Local\\RagLab-" + key)
    if not handle:
        raise Block()
    try:
        if wait(handle, 0) not in {0, 0x80}:
            raise Block()
        try:
            yield
        finally:
            release(handle)
    finally:
        close(handle)


def digest(stream, output=None, check=lambda: None):
    value = hashlib.sha256()
    stream.seek(0)
    while chunk := stream.read(1024 * 1024):
        check()
        value.update(chunk)
        if output is not None:
            output.write(chunk)
    return value.hexdigest()


def check_path(path, *, missing=False):
    with PATHS.pin(path, missing_ok=missing, directory_write_sharing=True, reject_aliases=True) as entry:
        return entry.info


def read_json(path):
    with PATHS.pin(path, directory_write_sharing=True, reject_aliases=True) as entry:
        if entry.info.link_count != 1:
            raise Block()
        return json.loads(entry.read_bytes(4 * 1024 * 1024))


def report(path, value):
    with PATHS.pin(path.parent, directory_write_sharing=True, reject_aliases=True) as parent:
        PATHS.publish_report(parent, path, serialize_json(value).encode("utf-8"), lambda: None)


def inventory(root, pinned=None):
    """Inventory ordinary objects; reject reparse entries and multiply linked files."""
    result = {}
    locked = nullcontext(pinned) if pinned is not None else PATHS.pin(
        root, directory_write_sharing=True, reject_aliases=True,
    )
    with locked as top:
        if not top.info.is_directory:
            raise Block()

        def walk(directory, depth):
            if depth > 40:
                raise Block()
            with os.scandir(native(directory)) as listing:
                for child in listing:
                    path = directory / child.name
                    relative = path.relative_to(root).as_posix()
                    if len(result) >= 16384:
                        raise Block()
                    with PATHS.pin_entry(path, directory_write_sharing=True) as entry:
                        if entry.path != path or entry.info.link_count != 1 and not entry.info.is_directory:
                            raise Block()
                        result[relative] = entry.info
                        if entry.info.is_directory:
                            walk(entry.path, depth + 1)

        walk(top.path, 0)
    return result


def close_handle(handle):
    close = bind(system_dll("kernel32.dll"), "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    close(handle)


def mkdir(root, expected_parent=None):
    with PATHS.pin(root.parent, directory_write_sharing=True, reject_aliases=True) as parent:
        if expected_parent is not None and parent.info.file_id != expected_parent.file_id:
            raise Block()
        handle = PATHS._create_owned_directory(parent, root.name)
        try:
            info = PATHS.identity(handle)
            if PATHS._canonical(handle) != root or not info.is_directory:
                raise Block()
            return info
        finally:
            close_handle(handle)


def owned_handle(path):
    kernel = system_dll("kernel32.dll")
    create = bind(kernel, "CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                  wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    handle = create(native(path), 0x80010000, 1, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


@contextmanager
def removable(path, expected):
    handle = owned_handle(path)
    close = bind(system_dll("kernel32.dll"), "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    try:
        current = PATHS.identity(handle)
        if (
            PATHS._canonical(handle) != path or current.file_id != expected.file_id
            or current.is_directory != expected.is_directory
        ):
            raise Block()
        if not current.is_directory and current.link_count != 1:
            raise Block()
        yield handle
    finally:
        close(handle)


def discard(root, expected, members):
    """Remove the enumerated objects by handle, leaving unexpected entries in place."""
    nested = {}
    for name, info in members.items():
        path = root / name
        nested.setdefault(path.parent, []).append((path, info))

    def delete(path, info):
        with removable(path, info) as handle:
            for child, child_info in nested.get(path, ()):
                delete(child, child_info)
            PATHS._discard_owned_file(handle)

    with PATHS.pin(root.parent, directory_write_sharing=True, reject_aliases=True):
        delete(root, expected)


class OwnedTree:
    """Track every file and directory from its creation handle."""

    def __init__(self, root):
        self.root = root
        self.info = mkdir(root)
        self.members = {}
        self.hashes = {}

    def _directory(self, relative):
        if not relative.parts:
            return self.info
        name = relative.as_posix()
        if name not in self.members:
            ancestor = self._directory(relative.parent)
            self.members[name] = mkdir(self.root / relative, ancestor)
        member = self.members[name]
        if not member.is_directory:
            raise Block()
        return member

    def write(self, relative, *, data=None, source=None, check=lambda: None):
        relative = Path(relative)
        name = relative.as_posix()
        if not safe_relative(name) or relative.is_absolute():
            raise Block()
        expected = self._directory(relative.parent)
        destination = self.root / relative
        with PATHS.pin(destination.parent, directory_write_sharing=True, reject_aliases=True) as parent:
            if expected.file_id != parent.info.file_id:
                raise Block()
            handle = PATHS._create_owned_file(destination)
            try:
                info = PATHS.identity(handle)
                if PATHS._canonical(handle) != destination or info.is_directory or info.link_count != 1:
                    raise Block()
                self.members[name] = info
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
                handle = None  # fd now owns the handle.
                with os.fdopen(fd, "wb") as output:
                    if source is not None:
                        value = digest(source, output, check)
                    else:
                        output.write(data)
                        value = hashlib.sha256(data).hexdigest()
                    output.flush()
                    os.fsync(output.fileno())
                self.hashes[name] = value
            finally:
                if handle is not None:
                    close_handle(handle)

    def json(self, name, value):
        self.write(name, data=serialize_json(value).encode("utf-8"))

    @contextmanager
    def checked_members(self, root):
        with ExitStack() as pins:
            observed = inventory(root.path, pinned=root)
            if observed.keys() != self.members.keys():
                raise Block()
            for name, expected in self.members.items():
                entry = pins.enter_context(PATHS.pin_entry(
                    root.path / name, directory_write_sharing=True,
                ))
                if (
                    str(entry.path) != str(root.path / name)
                    or entry.info.file_id != expected.file_id
                    or entry.info.is_directory != expected.is_directory
                ):
                    raise Block()
                if not expected.is_directory and (
                    entry.info.link_count != 1 or digest(entry.stream) != self.hashes[name]
                ):
                    raise Block(4)
            yield

    @contextmanager
    def checked(self):
        with PATHS.pin(self.root, directory_write_sharing=True, reject_aliases=True) as root:
            if root.info.file_id != self.info.file_id:
                raise Block()
            with PATHS.watch_namespace(root) as unchanged, self.checked_members(root):
                unchanged()
                yield
                unchanged()

    def publish(self, destination):
        if self.root.parent != destination.parent:
            raise Block()
        with PATHS.pin(self.root.parent, directory_write_sharing=True, reject_aliases=True):
            with removable(self.root, self.info) as handle:
                staged = PinnedPath(self.root, self.info, handle)
                with PATHS.watch_namespace(staged, owned_for_rename=True) as unchanged:
                    with self.checked_members(staged):
                        unchanged()
                    # Close child handles before renaming the directory. The
                    # namespace watch stays armed until after post-rename validation.
                    PATHS._rename_file(handle, destination, replace=False)
                    try:
                        with self.checked_members(PinnedPath(destination, self.info, handle)):
                            unchanged()
                    except BaseException:
                        PATHS._rename_file(handle, self.root, replace=False)
                        raise

    def clean(self):
        current = check_path(self.root, missing=True)
        if current is None:
            return
        if current.file_id != self.info.file_id:
            raise Block()
        discard(self.root, self.info, self.members)


def copy_payload(files, stage, deadline):
    for name, member in files.items():
        relative = Path(name)
        if relative.parts[0].lower() in {
            ".venv", MARKER, INSTALL_REPORT, DATA_MARKER, "tmp", "cache", "runs", "logs",
        }:
            raise Block()
        stage.write(relative, source=member.stream, check=deadline.check)
    if not REQUIRED <= stage.hashes.keys():
        raise Block()
    return sorted(stage.hashes)


def build_venv(stage):
    # Windows CPython's venv uses stdlib-supplied redirectors and pyvenv.cfg.
    # Install only the files we can create exclusively and track by handle.
    base = Path(sys._base_executable).resolve(strict=True)
    redirectors = Path(venv.__file__).parent / "scripts" / "nt"
    for name in ("python.exe", "pythonw.exe"):
        source = PATHS.validate(redirectors / name)
        with PATHS.pin(source, reject_aliases=True) as launcher:
            stage.write(f".venv/Scripts/{name}", source=launcher.stream)
    config = (
        f"home = {base.parent}\n"
        "include-system-site-packages = false\n"
        f"version = {platform.python_version()}\n"
        f"executable = {base}\n"
    )
    stage.write(".venv/pyvenv.cfg", data=config.encode("utf-8"))
    stage.write(".venv/Lib/site-packages/raglab-local.pth", data=b"../../../src\n")


def safe_relative(value):
    return isinstance(value, str) and all(
        part not in {".", ".."} and path_segment_is_safe(part) for part in value.split("/")
    )


def read_install(root):
    value = read_json(root / MARKER)
    if (
        not isinstance(value, dict)
        or value.keys() - {"location"} != {
            "schema_version", "generation_fingerprint", "owner", "profile", "files", "directories", "payload",
        }
        or value["schema_version"] not in {1, 2} or (value["schema_version"] == 2) != ("location" in value) or not HEX.fullmatch(value["generation_fingerprint"])
        or not HEX.fullmatch(value["owner"]) or value["profile"] not in PROFILE
        or not isinstance(value["files"], dict) or not isinstance(value["directories"], list)
        or not isinstance(value["payload"], list)
        or any(not safe_relative(item) for item in [*value["files"], *value["directories"], *value["payload"]])
        or not REQUIRED <= set(value["payload"]) <= value["files"].keys()
        or any(not isinstance(item, str) or not HEX.fullmatch(item) for item in value["files"].values())
    ):
        raise Block()
    return value


def assert_managed_inventory(root, lock, pinned=None):
    current = inventory(root, pinned=pinned)
    allowed = lock["files"].keys() | set(lock["directories"]) | {MARKER, INSTALL_REPORT}
    if current.keys() - allowed:
        raise Block()
    if any(info.is_directory != (name in lock["directories"]) for name, info in current.items()):
        raise Block()
    return current


def matches(root, lock, current, payload):
    if current.keys() != lock["files"].keys() | set(lock["directories"]) | {MARKER, INSTALL_REPORT}:
        return False
    try:
        receipt = read_json(root / INSTALL_REPORT)
    except (ValueError, UnicodeError):
        return False
    if (
        not isinstance(receipt, dict) or receipt.keys() != SAFE_INSTALL_RECEIPT
        or receipt["schema_version"] != 1 or receipt["status"] != "PASS"
        or receipt["action"] not in {"Install", "Repair"} or receipt["profile"] != lock["profile"]
        or receipt["generation_fingerprint"] != lock["generation_fingerprint"]
    ):
        return False
    if payload.keys() != set(lock["payload"]) or any(
        lock["files"].get(name) != hash_value for name, hash_value in payload.items()
    ):
        return False
    for name, expected in lock["files"].items():
        with PATHS.pin(root / name, directory_write_sharing=True, reject_aliases=True) as item:
            if item.info.link_count != 1 or digest(item.stream) != expected:
                return False
    return True


def owner_location(root, data, identity):
    return {"install": normalized(root), "data": normalized(data), "identity": removal_identity(identity)}


def validate_location(marker, root, data, held, original):
    if marker["schema_version"] == 1:
        if held.path != original:
            raise Block()
    elif marker.get("location") != owner_location(root, data, held.info):
        raise Block()


def data_owner(held, owner, root, data):
    marker = read_json(held.path / DATA_MARKER)
    if not isinstance(marker, dict) or not (
        marker == {"schema_version": 1, "owner": owner}
        or marker.keys() == {"schema_version", "owner", "location"}
        and marker["schema_version"] == 2 and marker["owner"] == owner
    ):
        raise Block()
    validate_location(marker, root, data, held, data)
    current = inventory(held.path, pinned=held)
    if any(name.split("/")[0] not in DATA_DIRS | DATA_FILES for name in current):
        raise Block()
    if any(name in DATA_DIRS and not info.is_directory for name, info in current.items()):
        raise Block()
    return current


def clean_env():
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("PYTHON", "PIP_", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"))}


def staged_doctor(stage, bundle, data, profile):
    scratch = OwnedTree(data.parent / (".raglab-doctor-" + uuid.uuid4().hex))
    try:
        defaults = read_json(stage / "configs/security-defaults.json")
        for name in ("temp_root", "cache_root", "output_root"):
            value = defaults.get(name)
            if not isinstance(value, str):
                raise Block(5)
            relative = Path(value)
            if relative.is_absolute() or not safe_relative(relative.as_posix()):
                raise Block(5)
            scratch._directory(relative)
        # The bootstrap publishes only the approved generation over the child
        # process pipe. Doctor's filesystem probes run within owned scratch.
        marker = "RAGLAB-INSTALL-DOCTOR="
        code = (
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv.pop(1)); "
            "import raglab.cli as cli; "
            "cli.offline_config.CONFIG_DIRECTORY = Path(sys.argv.pop(1)); "
            f"cli._publish_report = lambda _paths, _source, _output, report: "
            f"print({marker!r} + str(report['generation_fingerprint'])); "
            "sys.exit(cli.main())"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", code, str(ROOT / "src"),
             str(stage / "configs"), "doctor", "--bundle", str(bundle), "--profile", profile,
             "--offline", "--report", str(scratch.root / "doctor-install.json")],
            cwd=scratch.root, env=clean_env(), check=False, timeout=300,
            stdout=subprocess.PIPE, text=True, errors="replace",
        )
        fingerprints = []
        for line in result.stdout.splitlines():
            if line.startswith(marker):
                fingerprints.append(line.removeprefix(marker))
            else:
                print(line)
        if result.returncode:
            raise Block(result.returncode if result.returncode in {2, 3, 4, 5, 6, 7} else 10)
        if len(fingerprints) != 1 or not HEX.fullmatch(fingerprints[0]):
            raise Block(4)
        return SimpleNamespace(generation_fingerprint=fingerprints[0])
    finally:
        primary_error = sys.exc_info()[0] is not None
        try:
            scratch.clean()
        except (Block, OSError, ValueError):
            if not primary_error:
                raise
            print("RAGLAB-INSTALL-003 doctor scratch requires local review", file=sys.stderr)


def move(source, destination, info):
    if source.parent != destination.parent:
        raise Block()
    with PATHS.pin(source.parent, directory_write_sharing=True, reject_aliases=True):
        with removable(source, info) as handle:
            PATHS._rename_file(handle, destination, replace=False)


def install(bundle, root, data, mode, profile):
    bundle, root, data = local_input(bundle), protected_root(root), protected_root(data)
    assert_disjoint(bundle, root, data, ROOT)
    with serialized(root), serialized(data):
        with PATHS.pin(root, missing_ok=True, directory_write_sharing=True, reject_aliases=True) as installed:
            with PATHS.pin(data, missing_ok=True, directory_write_sharing=True, reject_aliases=True) as owned:
                old_info, data_info = installed.info, owned.info
                lock = read_install(root) if old_info else None
                if lock:
                    validate_location(lock, root, data, installed, root)
                current = assert_managed_inventory(root, lock, installed) if lock else {}
                owner = lock["owner"] if lock else uuid.uuid4().hex * 2
                if data_info is not None:
                    data_owner(owned, owner, root, data)
                elif lock:
                    raise Block()
        stage_path = root.parent / (".raglab-build-" + uuid.uuid4().hex)
        backup = root.parent / (".raglab-backup-" + uuid.uuid4().hex)
        stage = None
        working = None
        copied = None
        unchanged = False

        def observe(files, fingerprint, _expires_at, deadline):
            nonlocal stage, unchanged, copied
            if lock and fingerprint == lock["generation_fingerprint"] and profile == lock["profile"]:
                source_hashes = {
                    Path(name).as_posix(): digest(member.stream, check=deadline.check)
                    for name, member in files.items()
                }
                with PATHS.pin(root, directory_write_sharing=True, reject_aliases=True) as held_root:
                    with PATHS.pin(data, directory_write_sharing=True, reject_aliases=True) as held_data:
                        if (
                            held_root.info.file_id != old_info.file_id
                            or held_data.info.file_id != data_info.file_id
                        ):
                            raise Block()
                        unchanged = matches(root, lock, current, source_hashes)
            if not unchanged:
                stage = OwnedTree(stage_path)
                copied = copy_payload(files, stage, deadline)

        try:
            verified = verify_bundle(bundle, WindowsCatalogVerifier(), observe_verified_payload=observe)
            if verified.status != "PASS":
                raise Block(verified.exit_code)
            if unchanged:
                print("RagLab install PASS (verified no-op)")
                return
            action = "Repair" if lock or mode == "Repair" else "Install"
            build_venv(stage)
            with stage.checked():
                doctor = staged_doctor(stage.root, bundle, data, profile)
                if doctor.generation_fingerprint != verified.generation_fingerprint:
                    raise Block(4)
            if data_info is None:
                working = OwnedTree(data)
                data_info = working.info
                working.json(DATA_MARKER, {
                    "schema_version": 2, "owner": owner, "location": owner_location(root, data, data_info),
                })
            new_lock = {
                "schema_version": 2, "owner": owner, "files": dict(stage.hashes),
                "location": owner_location(root, data, stage.info),
                "directories": sorted(name for name, info in stage.members.items() if info.is_directory),
                "profile": profile, "generation_fingerprint": verified.generation_fingerprint,
                "payload": copied,
            }
            stage.json(MARKER, new_lock)
            stage.json(INSTALL_REPORT, {
                "schema_version": 1, "status": "PASS", "action": action, "profile": profile,
                "generation_fingerprint": verified.generation_fingerprint,
            })
            with PATHS.pin(data, directory_write_sharing=True, reject_aliases=True) as held_data:
                if held_data.info.file_id != data_info.file_id:
                    raise Block()
                if old_info:
                    move(root, backup, old_info)
                try:
                    stage.publish(root)
                except BaseException:
                    if old_info:
                        move(backup, root, old_info)
                    raise
            stage = None
            working = None
            if old_info:
                discard(backup, old_info, current)
            print(f"RagLab install PASS ({action})")
        finally:
            with ExitStack() as cleanup:
                if stage is not None:
                    cleanup.callback(stage.clean)
                if working is not None:
                    cleanup.callback(working.clean)


REMOVAL_PLAN = ".removal-plan.json"


def removal_identity(info):
    return [info.volume, info.index.hex(), info.is_directory]


def valid_install_receipt(prior, lock):
    return (
        isinstance(prior, dict) and prior.keys() == SAFE_INSTALL_RECEIPT
        and prior["schema_version"] == 1 and prior["status"] == "PASS"
        and prior["action"] in {"Install", "Repair"} and prior["profile"] == lock["profile"]
        and prior["generation_fingerprint"] == lock["generation_fingerprint"]
    )


class RemovalRecoveryRequired(Block):
    """The live ownership evidence no longer supports automatic removal."""


def removal_record(root, data, root_path, data_path, policy):
    with ExitStack() as pins:
        installed, working = (
            pins.enter_context(PATHS.pin(path, directory_write_sharing=True, reject_aliases=True))
            for path in (root_path, data_path)
        )
        pins.enter_context(PATHS.pin(installed.path / MARKER, reject_aliases=True))
        pins.enter_context(PATHS.pin(working.path / DATA_MARKER, reject_aliases=True))
        lock = read_install(installed.path)
        validate_location(lock, root, data, installed, root)
        files = assert_managed_inventory(installed.path, lock, installed)
        work_files = data_owner(working, lock["owner"], root, data)
        prior = read_json(installed.path / INSTALL_REPORT)
        if not valid_install_receipt(prior, lock):
            raise Block()
        description = {
            "schema_version": 1, "generation_fingerprint": lock["generation_fingerprint"],
            "installed_objects": len(files), "working_objects": len(work_files),
        }
        identities = [removal_identity(held.info) for held in (installed, working)]
        roots = [normalized(root), normalized(data)]
        quarantine = []
        for original, identity in zip(roots, identities):
            material = json.dumps([lock["owner"], original, identity], ensure_ascii=True).encode("utf-8")
            quarantine.append(".raglab-remove-" + hashlib.sha256(material).hexdigest()[:32])
        plan = {
            "description": description, "policy": policy, "roots": roots,
            "quarantine": quarantine, "identities": identities,
            "members": [{name: removal_identity(info) for name, info in members.items()}
                        for members in (files, work_files)],
        }
        return plan, prior, ((installed.info, files), (working.info, work_files))


def prepare_removal(root, data, receipt, policy):
    plan, prior, snapshots = removal_record(root, data, root, data, policy)
    files = {"removal-manifest.json": plan["description"], REMOVAL_PLAN: plan}
    if policy == "KeepAuditReceipts":
        files[INSTALL_REPORT] = prior
    with PATHS.pin(receipt.parent, directory_write_sharing=True, reject_aliases=True) as parent:
        PATHS.publish_new_directory(parent, receipt.name, {
            name: serialize_json(value).encode("utf-8") for name, value in files.items()
        })
    return plan, snapshots


def quarantine_paths(plan, root, data, receipt):
    names = plan.get("quarantine") if isinstance(plan, dict) else None
    if not isinstance(names, list) or len(names) != 2 or any(
        not isinstance(name, str) or not re.fullmatch(r"\.raglab-remove-[0-9a-f]{32}", name)
        for name in names
    ):
        raise Block()
    paths = tuple(original.parent / name for original, name in zip((root, data), names))
    assert_disjoint(root, data, receipt, ROOT, *paths)
    return paths


def reauthenticate_removal(plan, root, data, receipt, policy):
    paths = quarantine_paths(plan, root, data, receipt)
    present = []
    for original, quarantine in zip((root, data), paths):
        found = [path for path in (original, quarantine) if check_path(path, missing=True) is not None]
        if len(found) != 1:
            raise RemovalRecoveryRequired()
        present.append(found[0])
    try:
        expected, prior, snapshots = removal_record(root, data, *present, policy)
        if expected != plan or read_json(receipt / "removal-manifest.json") != expected["description"]:
            raise RemovalRecoveryRequired()
        saved = receipt / INSTALL_REPORT
        if policy == "KeepAuditReceipts" and read_json(saved) != prior:
            raise RemovalRecoveryRequired()
        if policy == "Purge" and check_path(saved, missing=True) is not None:
            raise RemovalRecoveryRequired()
    except (Block, OSError, ValueError, TypeError, KeyError) as failure:
        raise RemovalRecoveryRequired() from failure
    return tuple(present), snapshots


def uninstall(root, data, policy):
    root, data = protected_root(root), protected_root(data)
    receipt = data.parent / (data.name + "-uninstall")
    assert_disjoint(root, data, receipt, ROOT)
    with serialized(root), serialized(data):
        plan_path = receipt / REMOVAL_PLAN
        if check_path(receipt, missing=True) is None:
            plan, snapshots = prepare_removal(root, data, receipt, policy)
            present = (root, data)
            plan_info = check_path(plan_path)
        else:
            with PATHS.pin(plan_path, missing_ok=True, reject_aliases=True) as held:
                plan_info = held.info
                if plan_info is not None:
                    if plan_info.link_count != 1 or plan_info.is_directory:
                        raise Block()
                    plan = json.loads(held.read_bytes(4 * 1024 * 1024))
            if plan_info is None:
                if check_path(root, missing=True) is None and check_path(data, missing=True) is None:
                    old = read_json(receipt / "uninstall-report.json")
                    if old.get("status") == "PASS" and old.get("negative_lookup") is True:
                        print("RagLab uninstall PASS (verified no-op)")
                        return
                raise Block()
            present, snapshots = reauthenticate_removal(plan, root, data, receipt, policy)
        quarantines = quarantine_paths(plan, root, data, receipt)
        for current, quarantine, (expected, _) in zip(present, quarantines, snapshots):
            if current != quarantine:
                move(current, quarantine, expected)
        for quarantine, (expected, members) in zip(quarantines, snapshots):
            discard(quarantine, expected, members)
        if any(check_path(path, missing=True) is not None for path in (root, data, *quarantines)):
            raise Block()
        report(receipt / "uninstall-report.json", {
            **plan["description"], "status": "PASS", "retention_policy": policy, "negative_lookup": True,
        })
        with PATHS.pin(receipt, directory_write_sharing=True, reject_aliases=True):
            with removable(plan_path, plan_info) as handle:
                PATHS._discard_owned_file(handle)
        print("RagLab uninstall PASS")


def vm_snapshot():
    evidence = acceptance_host_snapshot()
    if not evidence.get("supported") or not re.fullmatch(
        r"10\.0\.[0-9]{4,6}", evidence.get("windows_build", ""),
    ):
        raise Block(5)
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z", evidence.get("boot", "")):
        raise Block(5)
    return evidence


class ProcessMemory(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "peak_working_set", "working_set", "peak_paged", "paged",
            "peak_non_paged", "non_paged", "pagefile", "peak_pagefile",
        )
    ]


def process_peak():
    current = bind(system_dll("kernel32.dll"), "GetCurrentProcess", wintypes.HANDLE)
    counters = ProcessMemory()
    counters.size = ctypes.sizeof(counters)
    query = bind(system_dll("psapi.dll"), "GetProcessMemoryInfo", wintypes.BOOL,
                 wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD)
    if not query(current(), ctypes.byref(counters), counters.size):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.peak_working_set


def installed_command(root, data, *args):
    # Windows venv launchers redirect to a child interpreter. Measure that
    # interpreter from inside it so the launcher cannot mask working-set use.
    executable = root / ".venv/Scripts/python.exe"
    measure = (
        "import runpy, sys; scope = runpy.run_path(sys.argv.pop(1)); "
        "code = scope['raglab'](); "
        "print('RAGLAB-PROCESS-PEAK=' + str(scope['process_peak']())); sys.exit(code)"
    )
    # Load the installed package before the trusted instrumentation adds the
    # bootstrap's source to sys.path.
    measure = "import raglab.cli; " + measure
    result = subprocess.run(
        [str(executable), "-I", "-B", "-c", measure, str(Path(__file__).resolve()), *map(str, args)],
        cwd=data, env=clean_env(), check=False, timeout=300, capture_output=True, encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise Block(result.returncode if result.returncode in {2, 3, 4, 5, 6, 7} else 10)
    match = re.search(r"^RAGLAB-PROCESS-PEAK=([0-9]+)$", result.stdout, re.MULTILINE)
    if not match:
        raise Block(10)
    return int(match.group(1))


def synthesize_repro(root, data, state, demo_path, peak):
    result = read_json(demo_path / "result.json")
    result["regions"] = tuple(
        DemoRegion(region["region_id"], tuple(region["bbox"]), region["artifact_hash"])
        for region in result["regions"]
    )
    demo = DemoResult(**result)
    event_path = demo_path / "events.jsonl"
    with PATHS.pin(event_path, reject_aliases=True) as event:
        lines = event.read_bytes(16384).splitlines()
    events = tuple(replace(ProgressEvent(**json.loads(line)), peak_working_set_bytes=peak)
                   for line in lines)
    verified = read_json(data / "runs/verify-bundle.json")
    bundle = BundleReport(verified["status"], verified["approval_id"],
                          tuple(BundleFailure(**item) for item in verified["failures"]),
                          verified["generation_fingerprint"])
    machine = {"amd64": "x64", "arm64": "arm64", "x86": "x86"}.get(platform.machine().lower())
    lcid = bind(system_dll("kernel32.dll"), "GetUserDefaultLCID", wintypes.DWORD)()
    loc = locale.windows_locale.get(lcid)
    offset = datetime.now().astimezone().strftime("%z")
    environment = {
        "os_build": state["windows_build"], "cpu": machine, "ram_tier": state["profile"],
        "python": platform.python_version(), "locale": loc.replace("_", "-") if loc else None,
        "timezone": offset[:3] + ":" + offset[3:], "power_plan": "unknown",
    }
    with PATHS.pin(root / "fixtures/synthetic-mini/expected.json", reject_aliases=True) as fixture:
        manifest_hash = digest(fixture.stream)
    inputs = ReproInputs(
        bundle_report=bundle, config=load_profile_config(state["profile"]),
        doctor_report=read_doctor_report(data / "runs/doctor.json"), demo_result=demo,
        environment=environment, progress=events,
        deletion_report=read_json(demo_path / "deletion-smoke-report.json"),
        dataset=ApprovedDatasetManifest("synthetic-mini", "1.0", 1, manifest_hash),
    )
    destination = data / "runs/repro"
    if build_repro_bundle(inputs, destination).status != "PASS":
        raise Block(7)
    return destination


def long_path_probe(data):
    label = "RagLab \uD55C\uAD6D\uC5B4 \u65E5\u672C\u8A9E"
    name = label + "-" + "x" * (220 - len(label) - 1)
    path = data / "tmp" / name
    mkdir(path)
    with PATHS.pin(path, directory_write_sharing=True, reject_aliases=True) as member:
        if not member.info.is_directory or len(member.path.name) != 220:
            raise Block(5)
    report(path / "probe.json", {"synthetic": True})
    if read_json(path / "probe.json") != {"synthetic": True}:
        raise Block(5)


def active_check(bundle, data, profile, approval_id, endpoint, adapter):
    if adapter is None:
        raise Block()

    class BoundCanary:
        def verify_denied(self, signed_endpoint, signed_approval):
            if signed_endpoint != endpoint or signed_approval != approval_id:
                return False
            return adapter.verify_denied(signed_endpoint, signed_approval)

    result = raglab([
        "doctor", "--bundle", str(bundle), "--profile", profile, "--offline",
        "--report", str(data / "runs/doctor-canary.json"), "--active-egress-canary",
        "--approval-id", approval_id,
    ], active_canary=BoundCanary())
    if result:
        raise Block(result)


def acceptance(bundle, output, profile, resume, approval_id, endpoint, security_canary=None):
    if admin_token():
        raise Block()
    if bool(approval_id) != bool(endpoint):
        raise Block(2)
    if approval_id and not resume:
        raise Block(2)
    if approval_id and security_canary is None:
        raise Block(3)
    bundle, output = local_input(bundle), protected_root(output)
    assert_disjoint(bundle, output, ROOT)
    root = output / "installed"
    data = output / "working data \uD55C\uAD6D\uC5B4 \u65E5\u672C\u8A9E"
    receipt = data.parent / (data.name + "-uninstall")
    result_path = output / "acceptance-report.json"
    hold = vm_snapshot()
    started = time.monotonic()
    with serialized(output):
        if resume:
            state = read_json(result_path)
            if state["status"] != "PENDING_REBOOT" or state["profile"] != profile:
                raise Block()
            if state["boot_before"] >= hold["boot"]:
                raise Block(5)
        else:
            if check_path(output, missing=True) is not None:
                raise Block(2)
            mkdir(output)
            state = {
                "schema_version": 1, "profile": profile, "status": "RUNNING",
                "standard_user": True, "windows_build": hold["windows_build"],
                "defender": hold["defender"], "edr": hold["edr"], "boot_before": hold["boot"],
                "active_egress_canary": "NOT_RUN_REQUIRES_SECURITY_APPROVAL",
                "steps": {}, "active_elapsed_ms": 0, "peak_cli_working_set_bytes": 0,
            }

        def step(name, task):
            before = time.monotonic()
            result = task()
            if type(result) is int:
                state["peak_cli_working_set_bytes"] = max(state["peak_cli_working_set_bytes"], result)
                if result > (12 if profile == "local16" else 26) * 1024**3:
                    raise Block(5)
            state.setdefault("step_elapsed_ms", {})[name] = int((time.monotonic() - before) * 1000)
            state["steps"][name] = "PASS"
            return result

        try:
            if resume:
                state["boot_after"] = hold["boot"]
                step("restart_check", lambda: installed_command(
                    root, data, "doctor", "--bundle", bundle, "--profile", profile, "--offline",
                    "--report", data / "runs/doctor-resume.json",
                ))
                restarted = read_doctor_report(data / "runs/doctor-resume.json")
                if restarted.generation_fingerprint != state["generation_fingerprint"]:
                    raise Block(4)
                if approval_id:
                    previous = Path.cwd()
                    try:
                        os.chdir(data)
                        step("active_egress_canary", lambda: active_check(
                            bundle, data, profile, approval_id, endpoint, security_canary,
                        ))
                    finally:
                        os.chdir(previous)
                    state["active_egress_canary"] = "PASS"
                step("uninstall", lambda: uninstall(root, data, "KeepAuditReceipts"))
                negative = read_json(receipt / "uninstall-report.json")
                if negative["negative_lookup"] is not True:
                    raise Block()
                state["steps"]["negative_lookup"] = "PASS"
                state["status"] = "PASS"
                return 0
            step("install", lambda: install(bundle, root, data, "Install", profile))
            runs = data / "runs"
            step("verify_bundle", lambda: installed_command(
                root, data, "verify-bundle", "--bundle", bundle, "--report", runs / "verify-bundle.json",
            ))
            step("doctor", lambda: installed_command(
                root, data, "doctor", "--bundle", bundle, "--profile", profile, "--offline",
                "--report", runs / "doctor.json",
            ))
            step("long_unicode_path", lambda: long_path_probe(data))
            demo_path = runs / "synthetic-demo"
            peak = step("demo", lambda: installed_command(
                root, data, "demo", "--profile", profile, "--doctor-report", runs / "doctor.json",
                "--output", demo_path,
            ))
            if time.monotonic() - started > 1800:
                raise Block(5)
            repro = synthesize_repro(root, data, state, demo_path, peak)
            step("repro_verify", lambda: installed_command(
                root, data, "reproduce", "--bundle", repro, "--verify-only",
            ))
            state["generation_fingerprint"] = read_install(root)["generation_fingerprint"]
            damaged = root / "fixtures/synthetic-mini/expected.json"
            discard(damaged, check_path(damaged), {})
            step("repair", lambda: install(bundle, root, data, "Repair", profile))
            if read_install(root)["generation_fingerprint"] != state["generation_fingerprint"]:
                raise Block(4)
            state["inventory"] = {
                "installed_objects": len(inventory(root)), "working_objects": len(inventory(data)),
                "repro_objects": len(inventory(repro)),
            }
            state["status"] = "PENDING_REBOOT"
            print("Reboot the approved VM and rerun with -Resume.")
            return 8
        except (Block, OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError):
            state["status"] = "BLOCK"
            raise
        finally:
            state["active_elapsed_ms"] += int((time.monotonic() - started) * 1000)
            report(result_path, state)


def main(argv=None, *, security_canary=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="operation", required=True)
    setup = commands.add_parser("install")
    setup.add_argument("bundle", type=Path)
    setup.add_argument("root", type=Path)
    setup.add_argument("data", type=Path)
    setup.add_argument("mode", choices=("Install", "Repair"))
    setup.add_argument("profile", choices=PROFILE)
    removal = commands.add_parser("uninstall")
    removal.add_argument("root", type=Path)
    removal.add_argument("data", type=Path)
    removal.add_argument("policy", choices=("Purge", "KeepAuditReceipts"))
    test = commands.add_parser("acceptance")
    test.add_argument("bundle", type=Path)
    test.add_argument("output", type=Path)
    test.add_argument("profile", choices=PROFILE)
    test.add_argument("--resume", action="store_true")
    test.add_argument("--approval-id")
    test.add_argument("--canary-endpoint")
    args = vars(parser.parse_args(argv))
    operation = args.pop("operation")
    category = {"install": "INSTALL", "uninstall": "UNINSTALL", "acceptance": "ACCEPTANCE"}[operation]
    if sys.version_info[:2] != (3, 11) or sys.platform != "win32":
        print(f"RAGLAB-{category}-002 requires approved Windows Python 3.11", file=sys.stderr)
        return 2
    if admin_token():
        print(f"RAGLAB-{category}-003 requires a standard-user token", file=sys.stderr)
        return 3
    if operation == "acceptance":
        args["endpoint"] = args.pop("canary_endpoint")
        args["security_canary"] = security_canary
    try:
        result = {"install": install, "uninstall": uninstall, "acceptance": acceptance}[operation](**args)
        return result or 0
    except RemovalRecoveryRequired as failure:
        code = failure.number
        print("RAGLAB-UNINSTALL-003 local recovery required. See the operator offline installation guide.", file=sys.stderr)
        return code
    except Block as failure:
        code = failure.number
    except (FileNotFoundError, PermissionError, ValueError, TypeError, KeyError):
        code = 2
    except (OSError, RuntimeError, subprocess.SubprocessError):
        code = 10
    print(f"RAGLAB-{category}-{code:03d} operation stopped", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())

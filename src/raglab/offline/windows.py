import ctypes
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from ctypes import wintypes as win
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from raglab.offline.config import EffectiveConfig
from raglab.offline.ports import (
    CatalogDecision,
    EgressPolicy,
    SystemSnapshot,
    VerificationDeadline,
    normalize_thumbprint,
)
from raglab.offline.windows_names import WindowsFilenameIndex, windows_name_compare
from raglab.offline.windows_paths import PinnedPath, WindowsLocalPaths, bind, system_dll


_POLICY_KEY = r"SOFTWARE\Policies\RagLab\OfflineToolkit"
_POLICY_VALUE = "ApprovedPublisherThumbprints"
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_SHA1_OID = b"1.3.14.3.2.26"
_SHA256_OID = b"2.16.840.1.101.3.4.2.1"
_AUTHENTICATED_ATTRIBUTE = 0x10000000

# Windows SDK WinTrust.h and SoftPub.h
_GENERIC_VERIFY_V2 = (ctypes.c_ubyte * 16).from_buffer_copy(
    UUID("00aac56b-cd44-11d0-8cc2-00c04fc295ee").bytes_le
)
_WTD_UI_NONE = 2
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_REVOKE_WHOLECHAIN = 1
_WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT = 0x00000080
_WTD_CACHE_ONLY_URL_RETRIEVAL = 0x00001000
_WTD_DISABLE_MD2_MD4 = 0x00002000


class MachinePublisherAuthority:
    def approved_thumbprints(self) -> frozenset[str]:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _POLICY_KEY, 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            values, value_type = winreg.QueryValueEx(key, _POLICY_VALUE)
        if value_type != winreg.REG_MULTI_SZ or not isinstance(values, list) or not 0 < len(values) <= 128:
            raise ValueError("invalid machine publisher policy")
        signers = {normalize_thumbprint(value) for value in values}
        if None in signers:
            raise ValueError("invalid machine publisher policy")
        return frozenset(signers)


class _WinTrustFile(ctypes.Structure):
    _fields_ = [
        ("size", win.DWORD),
        ("path", win.LPCWSTR),
        ("file", win.HANDLE),
        ("known_subject", ctypes.c_void_p),
    ]


class _WinTrustData(ctypes.Structure):
    _fields_ = [
        ("size", win.DWORD),
        ("policy_callback_data", ctypes.c_void_p),
        ("client_data", ctypes.c_void_p),
        ("ui_choice", win.DWORD),
        ("revocation_checks", win.DWORD),
        ("union_choice", win.DWORD),
        ("file", ctypes.POINTER(_WinTrustFile)),
        ("state_action", win.DWORD),
        ("state_data", win.HANDLE),
        ("url_reference", win.LPCWSTR),
        ("provider_flags", win.DWORD),
        ("ui_context", win.DWORD),
        ("signature_settings", ctypes.c_void_p),
    ]


class _ProviderCertificate(ctypes.Structure):
    # Only the stable prefix is consumed. WTHelperGetProvCertFromChain owns it.
    _fields_ = [("size", win.DWORD), ("certificate", ctypes.c_void_p)]


class _Blob(ctypes.Structure):
    _fields_ = [("size", win.DWORD), ("data", ctypes.c_void_p)]


class _Algorithm(ctypes.Structure):
    _fields_ = [("oid", ctypes.c_char_p), ("parameters", _Blob)]


class _AttributeType(ctypes.Structure):
    _fields_ = [("oid", ctypes.c_char_p), ("value", _Blob)]


class _IndirectData(ctypes.Structure):
    _fields_ = [("type", _AttributeType), ("algorithm", _Algorithm), ("digest", _Blob)]


class _CatalogMember(ctypes.Structure):
    _fields_ = [
        ("size", win.DWORD),
        ("tag", win.LPWSTR),
        ("file", win.LPWSTR),
        ("subject", ctypes.c_ubyte * 16),
        ("flags", win.DWORD),
        ("indirect", ctypes.POINTER(_IndirectData)),
        ("version", win.DWORD),
        ("reserved_number", win.DWORD),
        ("reserved_handle", win.HANDLE),
        ("encoded_indirect", _Blob),
        ("encoded_member", _Blob),
    ]


class _CatalogAttribute(ctypes.Structure):
    _fields_ = [
        ("size", win.DWORD),
        ("tag", win.LPWSTR),
        ("flags", win.DWORD),
        ("length", win.DWORD),
        ("value", ctypes.c_void_p),
        ("reserved", win.DWORD),
    ]


class _NativeCatalog:
    def __init__(self):
        trust = system_dll("wintrust.dll")
        crypto = system_dll("crypt32.dll")
        self.verify_trust = bind(
            trust, "WinVerifyTrust", win.LONG, win.HWND, ctypes.c_void_p, ctypes.POINTER(_WinTrustData),
        )
        self.provider_data = bind(
            trust, "WTHelperProvDataFromStateData", ctypes.c_void_p, win.HANDLE,
        )
        self.provider_signer = bind(
            trust, "WTHelperGetProvSignerFromChain", ctypes.c_void_p, ctypes.c_void_p,
            win.DWORD, win.BOOL, win.DWORD,
        )
        self.provider_cert = bind(
            trust, "WTHelperGetProvCertFromChain", ctypes.POINTER(_ProviderCertificate),
            ctypes.c_void_p, win.DWORD,
        )
        self.cert_property = bind(
            crypto, "CertGetCertificateContextProperty", win.BOOL, ctypes.c_void_p,
            win.DWORD, ctypes.c_void_p, ctypes.POINTER(win.DWORD),
        )
        member = ctypes.POINTER(_CatalogMember)
        attribute = ctypes.POINTER(_CatalogAttribute)
        self.cat_open = bind(
            trust, "CryptCATOpen", win.HANDLE, win.LPWSTR, win.DWORD, ctypes.c_void_p,
            win.DWORD, win.DWORD,
        )
        self.cat_close = bind(trust, "CryptCATClose", win.BOOL, win.HANDLE)
        self.next_member = bind(trust, "CryptCATEnumerateMember", member, win.HANDLE, member)
        self.next_attr = bind(trust, "CryptCATEnumerateAttr", attribute, win.HANDLE, member, attribute)
        self.acquire = bind(
            trust, "CryptCATAdminAcquireContext2", win.BOOL, ctypes.POINTER(win.HANDLE),
            ctypes.c_void_p, win.LPCWSTR, ctypes.c_void_p, win.DWORD,
        )
        self.release = bind(trust, "CryptCATAdminReleaseContext", win.BOOL, win.HANDLE, win.DWORD)
        self.calc_hash = bind(
            trust, "CryptCATAdminCalcHashFromFileHandle2", win.BOOL, win.HANDLE, win.HANDLE,
            ctypes.POINTER(win.DWORD), ctypes.c_void_p, win.DWORD,
        )

    def signer(self, catalog: PinnedPath, deadline: VerificationDeadline) -> str | None:
        file = _WinTrustFile(
            ctypes.sizeof(_WinTrustFile), "\\\\?\\" + str(catalog.path), catalog.handle, None,
        )
        data = _WinTrustData(
            size=ctypes.sizeof(_WinTrustData),
            ui_choice=_WTD_UI_NONE,
            revocation_checks=_WTD_REVOKE_WHOLECHAIN,
            union_choice=_WTD_CHOICE_FILE,
            file=ctypes.pointer(file),
            state_action=_WTD_STATEACTION_VERIFY,
            provider_flags=(
                _WTD_CACHE_ONLY_URL_RETRIEVAL
                | _WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT
                | _WTD_DISABLE_MD2_MD4
            ),
        )
        try:
            status = deadline.call(self.verify_trust, _INVALID_HANDLE, _GENERIC_VERIFY_V2, ctypes.byref(data))
            if status != 0 or not data.state_data:
                return None
            provider = deadline.call(self.provider_data, data.state_data)
            signer = deadline.call(self.provider_signer, provider, 0, False, 0) if provider else None
            certificate = deadline.call(self.provider_cert, signer, 0) if signer else None
            if not certificate or not certificate.contents.certificate:
                return None
            thumbprint = (ctypes.c_ubyte * 20)()
            length = win.DWORD(len(thumbprint))
            success = deadline.call(
                self.cert_property, certificate.contents.certificate, 3, thumbprint, ctypes.byref(length),
            )
            return bytes(thumbprint).hex().upper() if success and length.value == len(thumbprint) else None
        finally:
            if data.state_data:
                data.state_action = _WTD_STATEACTION_CLOSE
                self.verify_trust(_INVALID_HANDLE, _GENERIC_VERIFY_V2, ctypes.byref(data))

    def _member_name(self, handle, member, deadline: VerificationDeadline) -> str:
        attr = None
        name = None
        for _ in range(16):
            attr = deadline.call(self.next_attr, handle, member, attr)
            if not attr:
                if name is not None:
                    return name
                break
            entry = attr.contents
            if entry.tag == "FilePath":
                if (
                    name is not None
                    or entry.size != ctypes.sizeof(_CatalogAttribute)
                    or not (entry.flags & _AUTHENTICATED_ATTRIBUTE)
                    or not entry.value
                    or not 2 < entry.length <= 65536
                    or entry.length % 2
                ):
                    break
                encoded = ctypes.string_at(entry.value, entry.length)
                path = encoded.decode("utf-16-le")
                if not path.endswith("\0") or "\0" in path[:-1]:
                    break
                name = path[:-1]
        raise ValueError("malformed catalog member path")

    def members(
        self, catalog: PinnedPath, count: int, deadline: VerificationDeadline,
    ) -> dict[str, str]:
        # CryptCATOpen parses a file already pinned and size-checked by the caller.
        deadline.check()
        handle = self.cat_open("\\\\?\\" + str(catalog.path), 4, None, 0, 0)
        if handle in {None, _INVALID_HANDLE}:
            raise OSError("catalog open failed")
        try:
            deadline.check()
            sha256: dict[str, str] = {}
            compatibility: set[str] = set()
            member = None
            for _ in range(count * 2 + 1):
                member = deadline.call(self.next_member, handle, member)
                if not member:
                    names = WindowsFilenameIndex(sha256)
                    if compatibility:
                        WindowsFilenameIndex(compatibility)
                        if len(compatibility) != len(sha256) or any(
                            names.find(name) is None for name in compatibility
                        ):
                            raise ValueError("catalog compatibility inventory mismatch")
                    return sha256
                current = member.contents
                if current.size != ctypes.sizeof(_CatalogMember) or not current.indirect:
                    raise ValueError("malformed catalog member")
                indirect = current.indirect.contents
                key = self._member_name(handle, member, deadline).replace("\\", "/")
                if indirect.algorithm.oid == _SHA1_OID:
                    if key in compatibility:
                        raise ValueError("duplicate compatibility catalog member")
                    compatibility.add(key)
                elif (
                    indirect.algorithm.oid == _SHA256_OID
                    and indirect.digest.size == 32
                    and indirect.digest.data
                    and key not in sha256
                ):
                    sha256[key] = ctypes.string_at(indirect.digest.data, 32).hex()
                else:
                    raise ValueError("invalid or duplicated catalog digest")
            raise ValueError("catalog entry limit exceeded")
        finally:
            self.cat_close(handle)

    def member_hashes_match(
        self,
        files: Mapping[str, PinnedPath],
        members: Mapping[str, str],
        deadline: VerificationDeadline,
    ) -> bool:
        names = WindowsFilenameIndex(members)
        if len(files) != len(members) or any(names.find(name) is None for name in files):
            return False
        admin = win.HANDLE()
        deadline.check()
        try:
            if not self.acquire(ctypes.byref(admin), None, "SHA256", None, 0):
                return False
            deadline.check()
            for name, member in files.items():
                member.stream.seek(0)
                sha = (ctypes.c_ubyte * 32)()
                size = win.DWORD(len(sha))
                if not deadline.call(self.calc_hash, admin, member.handle, ctypes.byref(size), sha, 0):
                    return False
                if size.value != len(sha) or bytes(sha).hex() != members[names.find(name)]:
                    return False
            return True
        finally:
            if admin.value:
                self.release(admin, 0)


class WindowsCatalogVerifier:
    def verify(
        self,
        catalog: PinnedPath,
        files: Mapping[str, PinnedPath],
        deadline: VerificationDeadline,
    ) -> CatalogDecision:
        failure = CatalogDecision(False, None, "catalog signature or membership verification failed")
        try:
            native = deadline.call(_NativeCatalog)
            signer = native.signer(catalog, deadline)
            if signer is None:
                return failure
            members = native.members(catalog, len(files), deadline)
            if not native.member_hashes_match(files, members, deadline):
                return failure
        except (OSError, ValueError):
            return failure
        return CatalogDecision(True, signer, None, members)


_POWERSHELL_SETUP = r"""
$env:PSModulePath = "$PSHOME\Modules"
$ErrorActionPreference = 'Stop'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw 'Unsupported Windows PowerShell'
}
"""
_SYSTEM_SCRIPT = _POWERSHELL_SETUP + r"""
$os = CimCmdlets\Get-CimInstance -ClassName Win32_OperatingSystem
$physical = [uint64]0
foreach ($module in @(CimCmdlets\Get-CimInstance -ClassName Win32_PhysicalMemory)) {
    $physical += [uint64]$module.Capacity
}
$result = @{
    supported = [bool]($os.ProductType -eq 1 -and $os.Version -match '^10\.0\.[0-9]+$')
    physical_bytes = $physical
    available_bytes = [uint64]$os.FreePhysicalMemory * 1024
}
$result | ConvertTo-Json -Compress
"""
_FIREWALL_SCRIPT = _POWERSHELL_SETUP + r"""
$profiles = @(NetSecurity\Get-NetFirewallProfile -PolicyStore ActiveStore)
$rules = @(NetSecurity\Get-NetFirewallRule -PolicyStore ActiveStore)
$ipsec = @(NetSecurity\Get-NetIPsecRule -PolicyStore ActiveStore)
$connections = @(NetConnection\Get-NetConnectionProfile)
$required = @('Domain', 'Private', 'Public')
$blocked = @($profiles | Where-Object { $_.Enabled -eq 'True' -and $_.DefaultOutboundAction -eq 'Block' })
$allNames = @($profiles | ForEach-Object { $_.Name } | Sort-Object)
if ($profiles.Count -ne 3 -or ($allNames -join ',') -ne ($required -join ',')) {
    throw 'Effective firewall profiles unavailable'
}
if ($rules.Count -eq 0 -or @($rules | Where-Object {
    $_.Enabled -notin @('True', 'False') -or $_.Direction -notin @('Inbound', 'Outbound') -or
    $_.Action -notin @('Allow', 'Block')
}).Count -ne 0) {
    throw 'Effective firewall rules unavailable'
}
$result = @{
    firewall_services_running = [bool](
        (Get-Service -Name 'MpsSvc').Status -eq 'Running' -and
        (Get-Service -Name 'BFE').Status -eq 'Running'
    )
    policy_exemptions_absent = [bool](
        @($profiles | Where-Object { @($_.DisabledInterfaceAliases | Where-Object { $_ }).Count -ne 0 }).Count -eq 0 -and
        @($ipsec | Where-Object { $_.Enabled -ne 'False' }).Count -eq 0
    )
    default_deny_profiles = $blocked.Count
    enabled_outbound_allows = @($rules | Where-Object {
        $_.Enabled -eq 'True' -and $_.Direction -eq 'Outbound' -and $_.Action -eq 'Allow'
    }).Count
    active_profiles_known = [bool](@($connections | Where-Object {
        $_.NetworkCategory -notin @('DomainAuthenticated', 'Private', 'Public')
    }).Count -eq 0)
}
$result | ConvertTo-Json -Compress
"""
_PROXY_VARIABLES = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "WS_PROXY", "WSS_PROXY",
})
_GIB = 1024**3


def _system_directory() -> Path:
    query = bind(system_dll("kernel32.dll"), "GetSystemDirectoryW", win.UINT, win.LPWSTR, win.UINT)
    buffer = ctypes.create_unicode_buffer(32768)
    length = query(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("trusted system directory unavailable")
    return Path(buffer.value)


def _unique_probe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError("duplicate probe field")
        fields[name] = value
    return fields


def _invalid_probe_constant(_value: str) -> None:
    raise ValueError("invalid probe number")


def _powershell_json(script: str) -> dict[str, object]:
    if script not in (_SYSTEM_SCRIPT, _FIREWALL_SCRIPT):
        raise ValueError("unknown local probe")
    system = _system_directory()
    powershell = system / "WindowsPowerShell" / "v1.0"
    result = subprocess.run(
        [str(powershell / "powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=system,
        env={
            "SystemRoot": str(system.parent),
            "WINDIR": str(system.parent),
            "PATH": str(system),
            "PSModulePath": str(powershell / "Modules"),
        },
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0 or result.stderr or len(result.stdout) > 16 * 1024:
        raise ValueError("Windows PowerShell probe failed")
    document = json.loads(
        result.stdout.decode("utf-8-sig"),
        object_pairs_hook=_unique_probe_object,
        parse_constant=_invalid_probe_constant,
    )
    if not isinstance(document, dict):
        raise ValueError("Windows PowerShell probe returned an invalid object")
    return document


class _WinHttpProxy(ctypes.Structure):
    _fields_ = [("access_type", win.DWORD), ("proxy", ctypes.c_void_p), ("bypass", ctypes.c_void_p)]


class _WinInetProxy(ctypes.Structure):
    _fields_ = [
        ("auto_detect", win.BOOL), ("auto_config_url", ctypes.c_void_p),
        ("proxy", ctypes.c_void_p), ("bypass", ctypes.c_void_p),
    ]


def _advanced_winhttp_proxy_is_direct() -> bool:
    system = _system_directory()
    result = subprocess.run(
        [str(system / "netsh.exe"), "winhttp", "show", "advproxy"],
        cwd=system,
        env={"SystemRoot": str(system.parent), "WINDIR": str(system.parent), "PATH": str(system)},
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0 or result.stderr or len(result.stdout) > 16 * 1024:
        return False
    start = result.stdout.find(b"{")
    if start < 0:
        return False
    # The heading is localized. The machine-readable settings object is JSON.
    settings = json.loads(
        result.stdout[start:].decode("utf-8"),
        object_pairs_hook=_unique_probe_object,
        parse_constant=_invalid_probe_constant,
    )
    flags = {"ProxyIsEnabled", "AutoConfigIsEnabled", "AutoDetect"}
    return (
        isinstance(settings, dict)
        and flags <= settings.keys() <= flags | {"PerUserProxySettings"}
        and all(settings[name] is False for name in flags)
        and type(settings.get("PerUserProxySettings")) is bool
    )


def _windows_proxy_is_direct() -> bool:
    # Read stored settings only. No PAC resolution, WPAD, DNS, or connectivity test.
    http = system_dll("winhttp.dll")
    free = bind(system_dll("kernel32.dll"), "GlobalFree", win.HGLOBAL, win.HGLOBAL)
    get_default = bind(http, "WinHttpGetDefaultProxyConfiguration", win.BOOL, ctypes.POINTER(_WinHttpProxy))
    get_user = bind(http, "WinHttpGetIEProxyConfigForCurrentUser", win.BOOL, ctypes.POINTER(_WinInetProxy))
    default = _WinHttpProxy()
    user = _WinInetProxy()
    try:
        return bool(
            get_default(ctypes.byref(default))
            and default.access_type == 1  # WINHTTP_ACCESS_TYPE_NO_PROXY
            and not (default.proxy or default.bypass)
            and get_user(ctypes.byref(user))
            and not (user.auto_detect or user.auto_config_url or user.proxy or user.bypass)
            and _advanced_winhttp_proxy_is_direct()
        )
    finally:
        for pointer in (default.proxy, default.bypass, user.auto_config_url, user.proxy, user.bypass):
            if pointer:
                free(pointer)


def _isolation_attestation_valid(document: object, now: datetime) -> bool:
    if not isinstance(document, dict) or document.keys() != {"schema_version", "scope", "expires_at"}:
        return False
    expiry = document["expires_at"]
    if not (
        type(document["schema_version"]) is int and document["schema_version"] == 1
        and document["scope"] == "full-workstation-egress-deny"
        and isinstance(expiry, str) and expiry.endswith("Z")
    ):
        return False
    try:
        expiration = datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return expiration > now
    except ValueError:
        return False


def _machine_isolation_is_attested() -> bool:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, _POLICY_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        raw, value_type = winreg.QueryValueEx(key, "IsolatedWorkstationAttestation")
    if value_type != winreg.REG_SZ or not isinstance(raw, str) or len(raw) > 4096:
        return False
    document = json.loads(raw, object_pairs_hook=_unique_probe_object, parse_constant=_invalid_probe_constant)
    return _isolation_attestation_valid(document, datetime.now(timezone.utc))


class WindowsEgressPolicyProbe:
    def inspect(self) -> EgressPolicy:
        try:
            policy = _powershell_json(_FIREWALL_SCRIPT)
            denied = (
                policy.get("firewall_services_running") is True
                and type(policy.get("default_deny_profiles")) is int and policy["default_deny_profiles"] == 3
                and type(policy.get("enabled_outbound_allows")) is int and policy["enabled_outbound_allows"] == 0
                and policy.get("active_profiles_known") is True
                and policy.get("policy_exemptions_absent") is True
                and not any(key.upper() in _PROXY_VARIABLES and value for key, value in os.environ.items())
                and _windows_proxy_is_direct()
                and _machine_isolation_is_attested()
            )
        except (OSError, ValueError, ImportError, RecursionError, subprocess.SubprocessError):
            denied = False
        return EgressPolicy("DENIED" if denied else "UNKNOWN")


def _is_within(candidate: Path, protected: Path) -> bool:
    return len(candidate.parts) >= len(protected.parts) and all(
        windows_name_compare(left, right) == 0 for left, right in zip(candidate.parts, protected.parts)
    )


def _smoke_check(operation: Callable[[], bool]) -> bool:
    try:
        return operation()
    except (OSError, ValueError, RuntimeError):
        return False


class WindowsSystemProbe:
    def __init__(
        self,
        config: EffectiveConfig,
        paths: WindowsLocalPaths,
        protected: tuple[Path, ...] = (),
    ):
        self.config = config
        self.paths = paths
        self.protected = protected

    def _working_root(self, key: str) -> Path:
        configured = self.config.values.get(key)
        if not isinstance(configured, str):
            raise ValueError("missing local working path")
        path = self.paths.validate(configured)
        with self.paths.pin(path, missing_ok=True, directory_write_sharing=True) as resolved:
            if any(_is_within(resolved.path, root) for root in self.protected):
                raise ValueError("working path collides with a protected path")
            return resolved.path

    def _round_trip(self, root: Path, *, atomic: bool = False, long_unicode: bool = False) -> bool:
        suffix = uuid4().hex
        name = f".raglab-doctor-{suffix}.json"
        if long_unicode:
            prefix = f".raglab-doctor-합성 日本語-{suffix}"
            name = prefix + "x" * (250 - len(prefix)) + ".json"
        stage = f".raglab-doctor-{suffix}.tmp" if atomic else None
        with ExitStack() as locks:
            directory = locks.enter_context(self.paths.pin(root, create_directories=True))
            if any(_is_within(directory.path, protected) for protected in self.protected):
                raise ValueError("working path collides with a protected path")
            if long_unicode and len(str(directory.path / name)) <= 260:
                return False
            self.paths.probe_file(
                directory, name, b'{"synthetic":true}\n', staged_name=stage, release_protection=locks.close,
            )
        return True

    def snapshot(self) -> SystemSnapshot:
        try:
            system = _powershell_json(_SYSTEM_SCRIPT)
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
            system = {}
        physical, available = system.get("physical_bytes"), system.get("available_bytes")
        try:
            roots = tuple(self._working_root(key) for key in ("temp_root", "cache_root", "output_root"))
            free_disk = min(shutil.disk_usage(root.anchor).free for root in roots) / _GIB
        except (OSError, ValueError, RuntimeError):
            roots, free_disk = (), None

        writable = long_unicode = atomic = deleted = False
        if roots:
            writable = all(_smoke_check(lambda: self._round_trip(root)) for root in roots)
            long_unicode = _smoke_check(lambda: self._round_trip(roots[0], long_unicode=True))
            atomic = _smoke_check(lambda: self._round_trip(roots[2], atomic=True))
            deleted = _smoke_check(lambda: self._round_trip(roots[0]))
        return SystemSnapshot(
            system.get("supported") is True,
            (sys.version_info.major, sys.version_info.minor),
            physical / _GIB if type(physical) is int and 0 < physical <= 2**64 else None,
            available / _GIB if type(available) is int and 0 < available <= 2**64 else None,
            free_disk, writable, long_unicode, atomic, deleted,
        )

import ctypes
from collections.abc import Mapping
from ctypes import wintypes as win
from uuid import UUID

from raglab.offline.ports import (
    CatalogDecision,
    VerificationDeadline,
    normalize_thumbprint,
)
from raglab.offline.windows_names import WindowsFilenameIndex
from raglab.offline.windows_paths import PinnedPath, bind, system_dll


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

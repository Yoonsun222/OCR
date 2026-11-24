import json
import os
import re
import subprocess
from pathlib import Path

from raglab.offline.ports import CatalogDecision, normalize_thumbprint


_CATALOG_PATH_ENV = "RAGLAB_CATALOG_PATH"
_CATALOG_TIMEOUT_SECONDS = 180
# Windows PowerShell reparses command-line arguments after -Command as source.
# Bind the path as an argument inside the constant command using a child-only
# environment value. Resolve the catalog context using literal filesystem paths.
_CATALOG_SCRIPT = r"""
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
    $signature = Get-AuthenticodeSignature -LiteralPath $args[0]
    Set-Location -LiteralPath ([IO.Path]::GetDirectoryName($args[0]))
    $membership = Test-FileCatalog -CatalogFilePath $args[0] -Path . -Detailed
    [pscustomobject]@{
        Status = [string] $signature.Status
        Thumbprint = $signature.SignerCertificate.Thumbprint
        MembershipStatus = [string] $membership.Status
        MembershipSignatureStatus = [string] $membership.Signature.Status
        MembershipThumbprint = $membership.Signature.SignerCertificate.Thumbprint
        HashAlgorithm = $membership.HashAlgorithm
        ManifestSha256 = $membership.CatalogItems['manifest.json']
        ApprovalSha256 = $membership.CatalogItems['approval.json']
    } | ConvertTo-Json -Compress
} ([Environment]::GetEnvironmentVariable('RAGLAB_CATALOG_PATH'))
"""


def _metadata_digest(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return None


class PowerShellCatalogVerifier:
    def verify(self, catalog_path: Path) -> CatalogDecision:
        failure = CatalogDecision(False, None, "catalog signature or membership verification failed")
        system_root = Path(os.environ.get("SystemRoot", ""))
        if not system_root.is_absolute() or not catalog_path.is_absolute():
            return failure
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        try:
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _CATALOG_SCRIPT],
                env={**os.environ, _CATALOG_PATH_ENV: str(catalog_path)},
                capture_output=True,
                encoding="utf-8",
                timeout=_CATALOG_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                return failure
            response = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return failure

        if not isinstance(response, dict):
            return failure
        signer = normalize_thumbprint(response.get("Thumbprint"))
        membership_signer = normalize_thumbprint(response.get("MembershipThumbprint"))
        manifest_digest = _metadata_digest(response.get("ManifestSha256"))
        approval_digest = _metadata_digest(response.get("ApprovalSha256"))
        if (
            response.get("Status") != "Valid"
            or response.get("MembershipStatus") != "Valid"
            or response.get("MembershipSignatureStatus") != "Valid"
            or response.get("HashAlgorithm") != "SHA256"
            or signer is None
            or signer != membership_signer
            or manifest_digest is None
            or approval_digest is None
        ):
            return failure
        return CatalogDecision(True, signer, None, manifest_digest, approval_digest)

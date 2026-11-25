# Offline install and removal

Install from a trusted, independently authenticated toolkit checkout or deployment medium containing scripts\offline, src and its runtime files. Obtain approved Python separately. The installer uses an installed Windows Python 3.11 runtime with the standard library and venv support. It can select an explicitly approved executable through -PythonPath. Runtime CLI and doctor accept Python 3.11 and 3.12. The PowerShell entry points use Windows PowerShell 5.1. Doctor accepts Windows workstation build 10.0 with a numeric build and the company may restrict that to specific supported images.

Use an unelevated PowerShell session on a managed local drive. Choose different, initially absent subdirectories beneath a writable existing parent. Keep installation and working data outside the bootstrap and approved bundle. Allow at least 10 GiB free on each working volume for doctor and space for the bundle and installation. Repair can hold the old installation plus one staged copy and both virtual environments.

```powershell
$bundle = 'D:\approved-bundle'
$install = Join-Path $env:LOCALAPPDATA 'RagLab'
$data = Join-Path $env:LOCALAPPDATA 'RagLab-Data'
.\scripts\offline\install.ps1 -BundlePath $bundle -InstallRoot $install -DataRoot $data
```

The source bundle contains the installed raglab.ps1, src, configs, and fixtures\synthetic-mini and the catalog controls described in [Windows bundle trust](WINDOWS-BUNDLE-TRUST.md). Keep the approved source available for doctor, verification, and repair. The installer creates an isolated venv, exposes the verified local src through a .pth entry, and runs offline doctor before installation publication. A successful install writes install-report.json inside the installation. Commands that operate on the installation should use its venv with -I -B from the data directory.

Repeat the same command for a verified no-op, or add -Mode Repair. A known missing or changed member triggers repair. Unknown members, unsafe links and ownership conflicts block repair so operators can inspect the location. Repair stages the new generation and keeps the previous installation available if publication fails. Keep the authenticated source handy and retry only after checking the return code and the directory state.

Stop toolkit sessions before running removal. The lifecycle mutex serializes lifecycle operations in the current Windows session. Removal withdraws the install and data directory names before deleting the enumerated managed objects and verifies negative lookup. Managed data includes tmp, cache, runs, and logs, including the synthetic demo and repro. Choose Purge or KeepAuditReceipts. Both policies emit a non-content removal manifest and uninstall-report.json in the sibling <DataRoot>-uninstall directory. KeepAuditReceipts also preserves the validated install receipt. Check negative_lookup and status in uninstall-report.json. If removal is interrupted, repeat the same command with the same roots and retention policy. Keep the temporary removal plan and any quarantined directories in place until the rerun completes.

```powershell
.\scripts\offline\uninstall.ps1 -InstallRoot $install -DataRoot $data -RetentionPolicy KeepAuditReceipts
```

Keep an earlier receipt in approved audit storage before reusing the same data root for another lifecycle.

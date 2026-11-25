# Start here

Use a company-controlled Windows workstation, an approved bundle, Windows PowerShell 5.1, and an [installed standard-user toolkit](OFFLINE-INSTALL.md). Start in the installation's working data directory. Set the PowerShell variables $bundle and $install to the approved bundle and installation directories.

Confirm the company approval and offline certificate trust with the security owner. Use the synthetic fixture in this walkthrough.

```powershell
$python = Join-Path $install '.venv\Scripts\python.exe'
& $python -I -B -m raglab verify-bundle --bundle $bundle --report .\runs\verify-bundle.json
& $python -I -B -m raglab doctor --profile local16 --offline --bundle $bundle --report .\runs\doctor.json
& $python -I -B -m raglab demo --profile local16 --doctor-report .\runs\doctor.json --output .\runs\synthetic-demo
```

Run the next command after exit 0. Budget 30 minutes (30분) from approved bundle receipt to the synthetic result. Inspect runs\synthetic-demo\summary.html and overlay.svg after the demo succeeds. The [error catalog](ERROR-CATALOG.md) gives the first checks for a failure.

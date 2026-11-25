# Reproducibility

Use the installed venv for both checks. From the working data directory:

```powershell
& $python -I -B -m raglab reproduce --bundle .\runs\repro --verify-only
& $python -I -B -m raglab reproduce --left .\runs\first-repro --right .\runs\second-repro --diff-env
```

The acceptance harness builds a synthetic repro with the toolkit API from the accepted bundle, effective profile, current doctor, demo result, progress and synthetic deletion report. Version 1 exports a run ID and doctor run ID, profile, pipeline and status, approved dataset ID/version/count/manifest digest, approval ID, generation fingerprint, redacted configuration, environment, metrics, stable error codes, network result, and deletion receipt. manifest.sha256 binds the bytes of the other eleven members. Verification checks exact inventory, hashes, schema and cross-document bindings. Compare environment snapshots for OS build, architecture, RAM tier, Python, locale, timezone and power plan, plus the artifact lock and effective configuration.

The replay check verifies internal bundle consistency. Retain company records that tie it to the approved generation and host. Share only the content-free repro export under approved retention. The demo result, overlay, and local HTML remain working outputs.

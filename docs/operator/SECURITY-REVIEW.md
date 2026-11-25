# Security review

The installation bootstrap is trusted media. Bundle identity requires the company publisher allowlist, signed metadata, current approval, approved payload licenses, catalog membership and manifest hashes. The installer copies payload default streams from held verification handles and checks the resulting bytes before publication. Consult [Windows bundle trust](WINDOWS-BUNDLE-TRUST.md) for offline revocation and DLL readiness requirements.

Use only the synthetic fixture before company document approval. Routine reports exclude document names and paths, document_text, ocr_text, crops, embeddings, questions, secrets, user names and absolute filesystem paths. Internal installation ownership and inventory metadata stay local. Export only reviewed receipts. A local negative lookup confirms that toolkit paths and enumerated content are gone after removal. Review any external backup, snapshot, or endpoint retention under its own deletion procedure.

## Clean VM

Use a security-approved disposable Windows workstation VM with a standard-user account, approved runtime, and security administrator provisioning already complete. Choose a fresh output root under an existing writable directory. Run phase one from trusted bootstrap media:

```powershell
.\scripts\offline\test-clean-vm.ps1 -BundlePath D:\approved-bundle -Profile local16 -OutputRoot C:\RagLabAcceptance
```

After PENDING_REBOOT, perform a normal VM reboot through the approved VM procedure. Rerun the same command with -Resume. The harness checks the OS boot time changed, runs doctor again against the same approved generation, uninstalls, and checks negative lookup. Review acceptance-report.json and the retained content-free uninstall receipt. The report includes the Windows build, a Defender probe result, the Microsoft EDR service observation, named step outcomes, aggregate file and directory counts, elapsed active time, and the measured CLI process working-set peak. Use site EDR evidence for other providers.

The default workflow uses passive network checks. The report records NOT_RUN_REQUIRES_SECURITY_APPROVAL for active egress. An active canary needs a dedicated company environment, explicit ApprovalId, the exact CanaryEndpoint from the signed site profile, and a security-owned adapter and sink. The checked-in PowerShell entry point blocks an active request. A trusted company launcher can inject its approved adapter into the bootstrap and request the canary during resume. Doctor checks the signed approval and endpoint before invoking the adapter. Use company evidence for the actual active check and verify the denial at the security-owned sink. Parser and synthetic checks on a development machine do not satisfy deployment acceptance.

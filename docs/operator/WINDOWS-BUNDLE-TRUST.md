# Windows bundle verification

Run the command on the supported Windows workstation and put the report outside the approved bundle.

```powershell
.\raglab.ps1 verify-bundle --bundle .\approved-bundle --report .\runs\verify-bundle.json
```

## Publisher provisioning

Security administrators manage the 64-bit registry view of `HKLM\SOFTWARE\Policies\RagLab\OfflineToolkit`. Provision `ApprovedPublisherThumbprints` as a `REG_MULTI_SZ` containing one to 128 approved Windows code-signing certificate thumbprints. Each value contains exactly 40 hexadecimal characters. Keep the policy writable only by administrators and the trusted deployment service. A missing or malformed policy blocks verification. Retrieve the actual thumbprints through the company's signing and certificate-management process.

The catalog signer must satisfy Windows Authenticode trust, the machine policy, and the signed list in `approval.json`. Windows signature verification uses cache-only URL retrieval with revocation checking. Provision the appropriate approved certificate chain and revocation material for offline verification. The tool reads policy and verifies signatures using Windows system libraries loaded from System32.

## Bundle layout

The root contains `manifest.json`, `approval.json`, `bundle.cat`, and the approved payload files. Generate a Windows catalog version 2 from the finalized metadata and payload tree. Include each metadata and payload file exactly once in its SHA-256 member inventory. Generate and sign the catalog outside the input tree, then put it at `bundle.cat`. The verifier understands the additional SHA-1 compatibility records emitted by Windows PowerShell 5.1. SHA-256 determines membership.

The manifest uses schema version 1 and exactly one entry per payload. Each entry contains a bundle-relative `path`, a 64-hex `sha256` of the complete file bytes, and `license_decision` set to `approved`. The three root control filenames are reserved. The approval supplies `approval_id`, an RFC 3339 `expires_at` with known offset, and a nonempty `allowed_signer_thumbprints` list. Both JSON files are UTF-8. Catalog membership binds the metadata read by the verifier and the Windows SIP hashes of the payload. The manifest checks the complete bytes independently.

Use direct local volumes that provide stable Windows file IDs. Bundle and report paths are checked before traversal. The supported paths exclude UNC paths, device paths, substituted or mapped drives, and reparse points. The verifier denies writes and deletes to the held filesystem objects while checking hashes and catalog membership. A recursive Windows namespace notification stays armed through the final decision. Any name or attribute change, overflow, or unavailable notification blocks the verified generation. The final inventory and file identities must match and the approval must still be current. Filename matching uses Windows ordinal comparison. A differing signed filename spelling must open the same held file. The report is flushed and atomically renamed by its exclusive staging handle in a pinned local report parent outside the bundle.

## Intake limits and handoff

| Limit | Default |
|---|---:|
| Each JSON metadata file | 4 MiB |
| Catalog file | 32 MiB |
| Manifest payload entries | 4,096 |
| Disk inventory entries including directories | 8,192 |
| Disk inventory depth below the bundle root | 32 levels |
| Aggregate payload bytes | 128 GiB |
| Cooperative verification deadline | 240 seconds |

The budget is checked between operations and before and after native cryptographic calls. An individual local WinVerifyTrust or CryptCAT call is non-preemptible. Measure worst-case verification latency with the approved bundles and storage used for production readiness.

A passing report carries a `generation_fingerprint`. It is SHA-256 over a canonical compact JSON map of sorted, original on-disk bundle-relative names to complete-file SHA-256 digests, including the catalog and both metadata files. An importer must reverify the current payload, match that fingerprint, and consume only the held default data streams of that validated inventory. A report describes the verified generation at the time of verification. Use the report only for that generation.

Exit 0 means PASS. Exit 2 indicates an invalid operator path or destination. Exit 3 indicates an approval or license block. Exit 4 indicates a bundle integrity or signature failure. A safe destination receives a report with relative paths. Path collisions and unsupported destinations are rejected before report publication.

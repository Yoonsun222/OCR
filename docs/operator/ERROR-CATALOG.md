# Error catalog

Exit 0 means the command completed and required checks passed. Exit 2 indicates invalid input, configuration, or destination. Exit 3 indicates a policy or approval block. Exit 4 indicates bundle integrity or signature failure. Exit 5 indicates a failed readiness gate. Exit 6 indicates a failed synthetic evidence contract. Exit 7 indicates failed repro validation. Exit 8 from the acceptance helper means a reboot and explicit resume remain pending. Exit 10 indicates an internal or system failure.

## RAGLAB-INPUT-001

Use separate, writable, direct local inputs and report destinations. Confirm that the report is outside the approved source.

## Bundle verification

- RAGLAB-BUNDLE-001 covers invalid, missing, or changing paths or inventory. Request the finalized approved bundle.
- RAGLAB-BUNDLE-002 indicates a full-file hash mismatch. Quarantine the bundle under company procedure.
- RAGLAB-BUNDLE-003 indicates a missing or untrusted catalog, invalid membership, or signing-policy failure. Contact the signing owner.
- RAGLAB-BUNDLE-004 indicates approval or publisher policy is missing, malformed, or expired. Check the deployment approval with its owner.
- RAGLAB-BUNDLE-005 indicates a payload license decision blocked the bundle. Request a new approved bundle.

## Configuration and readiness

- RAGLAB-CONFIG-001 indicates a schema error. Verify the keys and schema version.
- RAGLAB-CONFIG-002 indicates an invalid field value or path. Inspect effective configuration.
- RAGLAB-CONFIG-003 indicates an attempted change to a locked field or profile ceiling. Restore the approved defaults.
- RAGLAB-DOCTOR- identifies readiness errors. Inspect the failed check_id and its action in the doctor report.
- RAGLAB-MEM- identifies a resource limit or metric validation error. Confirm the approved profile and available RAM.

## Demo and reproducibility

- RAGLAB-DEMO-DOCTOR requires a current matching passing doctor report.
- RAGLAB-DEMO-OUTPUT requires a local output path with free output names.
- RAGLAB-DEMO-EVIDENCE requires evidence from the approved synthetic fixture.
- RAGLAB-REPRO- identifies a failed repro schema, inventory, or digest check. Rebuild from the approved source reports.

## Lifecycle

RAGLAB-INSTALL-, RAGLAB-UNINSTALL-, and RAGLAB-ACCEPTANCE- append a zero-padded numeric exit code. For 002, check input, paths, and runtime. For 003, check token, ownership, approval, and whether the requested operation conflicts with another run. For 004 or 005 inspect the applicable bundle or doctor report. Review the current filesystem and use an approved clean VM snapshot after a partial acceptance failure.

## RAGLAB-INTERNAL-001

Inspect the generated content-free diagnostic, then record its error code and stage with the toolkit owner.

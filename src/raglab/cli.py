import argparse
import json
import os
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from raglab.offline import config as offline_config
from raglab.offline.bundle import verify_bundle
from raglab.offline.doctor import (
    CanaryRequest,
    DoctorContext,
    VerifiedBundleEvidence,
    inspect_verified_bundle,
    run_doctor,
)
from raglab.offline.jsonio import serialize_json, write_json_atomic
from raglab.offline.ports import (
    ActiveEgressCanary, CatalogVerifier, EgressPolicyProbe, PublisherAuthority, SystemProbe,
)
from raglab.offline.windows import (
    MachineDllReadinessProbe, WindowsCatalogVerifier, WindowsEgressPolicyProbe, WindowsSystemProbe,
)
from raglab.offline.windows_names import windows_name_compare
from raglab.offline.windows_paths import PinnedPath, WindowsLocalPaths


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default error includes raw operands, which can be private paths.
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="raglab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify-bundle")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--report", type=Path, default=Path("runs/verify-bundle.json"))

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--bundle", type=Path, required=True)
    doctor.add_argument("--profile", choices=offline_config.PROFILES, required=True)
    doctor.add_argument("--offline", action="store_true", required=True)
    doctor.add_argument("--report", type=Path, default=Path("runs/doctor.json"))
    doctor.add_argument("--active-egress-canary", action="store_true")
    doctor.add_argument("--approval-id")

    config = subparsers.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate_config = config_commands.add_parser("validate")
    explain_config = config_commands.add_parser("explain")
    for command in (validate_config, explain_config):
        command.add_argument("--profile", choices=offline_config.PROFILES, required=True)
        command.add_argument("--run", type=Path)
    explain_config.add_argument("--output", type=Path, required=True)
    diff_config = config_commands.add_parser("diff")
    diff_config.add_argument("--left", type=Path, required=True)
    diff_config.add_argument("--right", type=Path, required=True)
    return parser


def _check_report_destination(paths: WindowsLocalPaths, source: PinnedPath, output: Path) -> None:
    with paths.pin(output, missing_ok=True, directory_write_sharing=True) as destination:
        source_parts = source.path.parts
        destination_parts = destination.path.parts
        if len(destination_parts) >= len(source_parts) and all(
            windows_name_compare(left, right) == 0
            for left, right in zip(source_parts, destination_parts)
        ):
            raise ValueError("bundle or report paths collide")
        if destination.info is not None and (
            destination.info.is_directory
            or (source.info is not None and destination.info.file_id == source.info.file_id)
        ):
            raise ValueError("bundle or report paths collide")


def _publish_report(paths: WindowsLocalPaths, source: PinnedPath, output: Path, value: dict) -> None:
    # The temporary file is created while every ancestor denies writes/deletes.
    # Windows atomic rename needs write sharing on the directory, so downgrade
    # only after staging into its pinned, now nonempty directory.
    with ExitStack() as protected:
        parent = protected.enter_context(paths.pin(output.parent, create_directories=True))
        destination = parent.path / output.name
        _check_report_destination(paths, source, destination)

        paths.publish_report(parent, destination, serialize_json(value).encode("utf-8"), protected.close)


def _verify_command(
    bundle: Path,
    output: Path,
    verifier: CatalogVerifier,
    authority: PublisherAuthority | None,
) -> int:
    paths = WindowsLocalPaths()
    try:
        bundle = paths.validate(bundle)
        output = paths.validate(output)
        with ExitStack() as pins:
            source = pins.enter_context(paths.pin(bundle, missing_ok=True, directory_write_sharing=True))
            parent = pins.enter_context(
                paths.pin(output.parent, missing_ok=True, directory_write_sharing=True)
            )
            destination = parent.path / output.name
            _check_report_destination(paths, source, destination)
            report = verify_bundle(source.path, verifier, publisher_authority=authority, local_paths=paths)
            _publish_report(paths, source, destination, asdict(report))
    except (OSError, RuntimeError, ValueError):
        print("Invalid local bundle or report destination.", file=sys.stderr)
        return 2

    print(f"Bundle verification {report.status}")
    return report.exit_code


def _doctor_command(
    args: argparse.Namespace,
    verifier: CatalogVerifier,
    authority: PublisherAuthority | None,
    system: SystemProbe | None,
    egress: EgressPolicyProbe | None,
    canary: ActiveEgressCanary | None,
) -> int:
    paths = WindowsLocalPaths()
    observations: list[VerifiedBundleEvidence] = []

    def observe(files, fingerprint, expires_at, deadline):
        observations.append(inspect_verified_bundle(files, fingerprint, expires_at, deadline))

    try:
        bundle = paths.validate(args.bundle)
        output = paths.validate(args.report)
        config_directory = paths.validate(offline_config.CONFIG_DIRECTORY)
        with ExitStack() as pins:
            source = pins.enter_context(paths.pin(bundle, missing_ok=True, directory_write_sharing=True))
            parent = pins.enter_context(paths.pin(output.parent, missing_ok=True, directory_write_sharing=True))
            config_root = pins.enter_context(paths.pin(
                config_directory, missing_ok=True, directory_write_sharing=True,
            ))
            destination = parent.path / output.name
            _check_report_destination(paths, source, destination)
            _check_report_destination(paths, config_root, destination)
            bundle_report = verify_bundle(
                source.path, verifier, publisher_authority=authority, local_paths=paths,
                observe_verified_payload=observe,
            )
            config = offline_config.load_profile_config(args.profile)
            context = DoctorContext(
                system if system is not None else WindowsSystemProbe(
                    config, paths, (source.path, config_root.path),
                ),
                egress if egress is not None else WindowsEgressPolicyProbe(),
                bundle_report, config,
                observations[0] if observations else None,
                CanaryRequest(args.active_egress_canary, args.approval_id, canary),
                MachineDllReadinessProbe(),
            )
            report = run_doctor(context)
            _publish_report(paths, source, destination, asdict(report))
    except (OSError, RuntimeError, ValueError):
        print("Invalid local doctor input or report destination.", file=sys.stderr)
        return 2

    for check in report.checks:
        print(f"{check.status:5} {check.check_id:23} {check.message}")
    if report.overall_status == "BLOCK":
        print("ACTION Review the doctor report; execution remains blocked.")
        return 5
    return 0


def _write_config_explanation(
    profile: str, run: Path | None, output: Path,
) -> offline_config.EffectiveConfig:
    if os.name == "nt":
        paths = WindowsLocalPaths()
        output = paths.validate(output)
        with ExitStack() as pins:
            config_root = pins.enter_context(paths.pin(
                paths.validate(offline_config.CONFIG_DIRECTORY), directory_write_sharing=True,
            ))
            _check_report_destination(paths, config_root, output)
            if run is not None:
                source = pins.enter_context(paths.pin(
                    paths.validate(run), missing_ok=True, directory_write_sharing=True,
                ))
                _check_report_destination(paths, source, output)
                run = source.path
            config = offline_config.load_profile_config(profile, run)
            _publish_report(paths, config_root, output, offline_config.explain_config(config))
            return config

    if str(output).startswith(("//", "\\\\")):
        raise ValueError("configuration output must be local")
    destination = output.resolve()
    if destination.is_relative_to(offline_config.CONFIG_DIRECTORY.resolve()):
        raise ValueError("configuration output collides with shipped configuration")
    if run is not None:
        source = run.resolve()
        if destination.is_relative_to(source) or (
            source.exists() and destination.exists() and source.samefile(destination)
        ):
            raise ValueError("configuration output collides with run input")
    config = offline_config.load_profile_config(profile, run)
    write_json_atomic(destination, offline_config.explain_config(config))
    return config


def _config_command(args: argparse.Namespace) -> int:
    try:
        if args.config_command == "diff":
            changes = offline_config.diff_config_reports(args.left, args.right)
            print(json.dumps(changes, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
            return 2 if changes else 0

        if args.config_command == "explain":
            config = _write_config_explanation(args.profile, args.run, args.output)
        else:
            config = offline_config.load_profile_config(args.profile, args.run)
    except (OSError, RuntimeError, ValueError):
        print("Invalid local configuration input or output.", file=sys.stderr)
        return 2

    print(serialize_json(offline_config.summarize_config(config)), end="")
    return config.exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog_verifier: CatalogVerifier | None = None,
    publisher_authority: PublisherAuthority | None = None,
    system_probe: SystemProbe | None = None,
    egress_probe: EgressPolicyProbe | None = None,
    active_canary: ActiveEgressCanary | None = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "verify-bundle":
        verifier = catalog_verifier if catalog_verifier is not None else WindowsCatalogVerifier()
        return _verify_command(args.bundle, args.report, verifier, publisher_authority)
    if args.command == "doctor":
        verifier = catalog_verifier if catalog_verifier is not None else WindowsCatalogVerifier()
        return _doctor_command(args, verifier, publisher_authority, system_probe, egress_probe, active_canary)
    if args.command == "config":
        return _config_command(args)
    return 0

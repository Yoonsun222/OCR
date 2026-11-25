import argparse
import json
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from raglab.offline import config as offline_config
from raglab.offline import demo as offline_demo
from raglab.offline.bundle import verify_bundle
from raglab.offline.doctor import (
    CanaryRequest,
    DoctorContext,
    VerifiedBundleEvidence,
    inspect_verified_bundle,
    run_doctor,
)
from raglab.offline.errors import OperatorError
from raglab.offline.jsonio import serialize_json, write_json_atomic
from raglab.offline.ports import (
    ActiveEgressCanary, CatalogVerifier, EgressPolicyProbe, PublisherAuthority, SystemProbe,
)
from raglab.offline.progress import Redactor
from raglab.offline.repro import verify_repro_bundle
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

    demo = subparsers.add_parser("demo")
    demo.add_argument("--profile", choices=offline_config.PROFILES, required=True)
    demo.add_argument("--doctor-report", type=Path, required=True)
    demo.add_argument("--output", type=Path)

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
    reproduce = subparsers.add_parser("reproduce")
    mode = reproduce.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--diff-env", action="store_true")
    reproduce.add_argument("--bundle", type=Path)
    reproduce.add_argument("--left", type=Path)
    reproduce.add_argument("--right", type=Path)
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


@contextmanager
def _local_input(stage: str, summary: str) -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError):
        raise OperatorError(
            code="RAGLAB-INPUT-001", stage=stage, summary=summary,
            observed="local input or destination could not be accepted",
            expected="valid local input and separate writable report destination",
            protected_state="command stopped", retry_safe=True,
            next_command=rf".\raglab.ps1 {stage} --help",
            docs="docs/operator/ERROR-CATALOG.md#raglab-input-001",
        ) from None


def _verify_command(
    bundle: Path,
    output: Path,
    verifier: CatalogVerifier,
    authority: PublisherAuthority | None,
) -> int:
    paths = WindowsLocalPaths()
    with ExitStack() as pins:
        with _local_input("verify-bundle", "Invalid local bundle or report destination."):
            bundle = paths.validate(bundle)
            output = paths.validate(output)
            source = pins.enter_context(paths.pin(bundle, missing_ok=True, directory_write_sharing=True))
            parent = pins.enter_context(
                paths.pin(output.parent, missing_ok=True, directory_write_sharing=True)
            )
            destination = parent.path / output.name
            _check_report_destination(paths, source, destination)
        report = verify_bundle(source.path, verifier, publisher_authority=authority, local_paths=paths)

        with _local_input("verify-bundle", "Invalid local bundle or report destination."):
            _publish_report(paths, source, destination, asdict(report))

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

    with ExitStack() as pins:
        with _local_input("doctor", "Invalid local doctor input or report destination."):
            bundle = paths.validate(args.bundle)
            output = paths.validate(args.report)
            config_directory = paths.validate(offline_config.CONFIG_DIRECTORY)
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

        with _local_input("doctor", "Invalid local doctor input or report destination."):
            _publish_report(paths, source, destination, asdict(report))

    for check in report.checks:
        print(f"{check.status:5} {check.check_id:23} {check.message}")
    if report.overall_status == "BLOCK":
        print("ACTION Review the doctor report; execution remains blocked.")
        return 5
    return 0


def _write_config_explanation(
    profile: str, run: Path | None, output: Path,
) -> offline_config.EffectiveConfig:
    stage = "config"
    summary = "Invalid local configuration input or output."
    if os.name == "nt":
        paths = WindowsLocalPaths()
        with ExitStack() as pins:
            with _local_input(stage, summary):
                output = paths.validate(output)
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
            explanation = offline_config.explain_config(config)
            with _local_input(stage, summary):
                _publish_report(paths, config_root, output, explanation)
            return config

    with _local_input(stage, summary):
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
    explanation = offline_config.explain_config(config)
    with _local_input(stage, summary):
        write_json_atomic(destination, explanation)
    return config


def _config_command(args: argparse.Namespace) -> int:
    if args.config_command == "diff":
        with _local_input("config", "Invalid local configuration input or output."):
            changes = offline_config.diff_config_reports(args.left, args.right)
        print(json.dumps(changes, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 2 if changes else 0

    if args.config_command == "explain":
        config = _write_config_explanation(args.profile, args.run, args.output)
    else:
        config = offline_config.load_profile_config(args.profile, args.run)
    print(serialize_json(offline_config.summarize_config(config)), end="")
    return config.exit_code


def _demo_command(args: argparse.Namespace) -> int:
    report = offline_demo.read_doctor_report(args.doctor_report)
    config = offline_config.load_profile_config(args.profile)
    output = args.output if args.output is not None else Path(config.values.get("output_root", "runs")) / "synthetic-demo"
    result = offline_demo.run_synthetic_demo(offline_demo.DemoContext(report, output, config))
    print(result.answer)
    for name in offline_demo.DEMO_OUTPUTS:
        print(name)
    return 0


def _reproduce_command(args: argparse.Namespace) -> int:
    if args.verify_only:
        report = verify_repro_bundle(args.bundle)
        print(f"Repro bundle verification {report.status}")
        return report.exit_code

    left = verify_repro_bundle(args.left)
    right = verify_repro_bundle(args.right)
    if left.environment is None or right.environment is None:
        print("Repro bundle verification BLOCK")
        return 7
    changes = {
        key: {"left": left.environment[key], "right": right.environment[key]}
        for key in left.environment
        if left.environment[key] != right.environment[key]
    }
    print(serialize_json(changes), end="")
    return 0


def _traceback_locations(exc: Exception) -> list[dict[str, object]]:
    # Capture the call stack without asking traceback to load or format source.
    package = Path(__file__).parent
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        filename = Path(trace.tb_frame.f_code.co_filename)
        frame: dict[str, object] = {"line": trace.tb_lineno}
        if filename.is_relative_to(package):
            frame["module"] = ".".join(filename.relative_to(package).with_suffix("").parts)
        frames.append(frame)
        trace = trace.tb_next
    return frames


def _write_diagnostic(output: Path, value: dict, args: argparse.Namespace | None) -> None:
    # All paths produced here are independent of CLI operands. Keep logs outside
    # the shipped configuration and any command input, including the bundle.
    sources = [offline_config.CONFIG_DIRECTORY]
    if getattr(args, "command", None) == "demo":
        sources.append(offline_demo.FIXTURE_DIRECTORY)
    for field in ("bundle", "run", "left", "right", "doctor_report"):
        operand = getattr(args, field, None)
        if operand is not None:
            sources.append(operand)
    if os.name == "nt":
        paths = WindowsLocalPaths()
        output = paths.validate(output)
        with ExitStack() as pins:
            for source in sources:
                protected = pins.enter_context(paths.pin(
                    paths.validate(source), missing_ok=True, directory_write_sharing=True,
                ))
                _check_report_destination(paths, protected, output)
            _publish_report(paths, protected, output, value)
    else:
        destination = output.resolve()
        if destination != output.absolute() or any(
            destination.is_relative_to(source.resolve()) for source in sources
        ):
            raise ValueError("unsafe diagnostics destination")
        write_json_atomic(destination, value)


def _report_error(
    error: OperatorError, args: argparse.Namespace | None, exc: Exception | None = None,
) -> int:
    redactor = Redactor()
    value = asdict(error)
    if exc is not None:
        value["traceback"] = _traceback_locations(exc)
    value = redactor.redact(value)
    output = Path("logs") / "diagnostics" / f"{uuid4().hex}.json"
    try:
        _write_diagnostic(output, value, args)
    except Exception:
        # Failure to write diagnostics must preserve the original exit code.
        location = "unavailable"
    else:
        location = str(output)
    for line in (
        f"{value['code']} {value['summary']}",
        f"Impact {value['protected_state']}",
        f"Next command {value['next_command']}",
        f"Diagnostics {location}",
    ):
        print(line.replace("\r", " ").replace("\n", " "), file=sys.stderr)
    return error.exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog_verifier: CatalogVerifier | None = None,
    publisher_authority: PublisherAuthority | None = None,
    system_probe: SystemProbe | None = None,
    egress_probe: EgressPolicyProbe | None = None,
    active_canary: ActiveEgressCanary | None = None,
) -> int:
    args = None
    try:
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
            if args.command == "reproduce":
                valid = (
                    args.bundle is not None and args.left is None and args.right is None
                    if args.verify_only else
                    args.bundle is None and args.left is not None and args.right is not None
                )
                if not valid:
                    parser.error("invalid reproduce mode operands")
        except SystemExit as exc:
            return int(exc.code)
        if args.command == "verify-bundle":
            verifier = catalog_verifier if catalog_verifier is not None else WindowsCatalogVerifier()
            return _verify_command(args.bundle, args.report, verifier, publisher_authority)
        if args.command == "doctor":
            verifier = catalog_verifier if catalog_verifier is not None else WindowsCatalogVerifier()
            return _doctor_command(args, verifier, publisher_authority, system_probe, egress_probe, active_canary)
        if args.command == "reproduce":
            return _reproduce_command(args)
        if args.command == "config":
            return _config_command(args)
        if args.command == "demo":
            return _demo_command(args)
        return 0
    except OperatorError as error:
        return _report_error(error, args)
    except Exception as exc:
        error = OperatorError(
            code="RAGLAB-INTERNAL-001", stage=getattr(args, "command", "startup"),
            summary="unexpected internal failure", observed="command interrupted",
            expected="command completion", protected_state="command stopped",
            retry_safe=False, next_command=r".\raglab.ps1 --help",
            docs="docs/operator/ERROR-CATALOG.md#raglab-internal-001", exit_code=10,
        )
        return _report_error(error, args, exc)

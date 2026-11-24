import argparse
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from raglab.offline.bundle import verify_bundle
from raglab.offline.jsonio import write_json_atomic
from raglab.offline.ports import CatalogVerifier
from raglab.offline.windows import PowerShellCatalogVerifier


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
    return parser


def _verify_command(bundle: Path, output: Path, verifier: CatalogVerifier) -> int:
    report = verify_bundle(bundle, verifier)
    try:
        destination = output.parent.resolve() / output.name
        if report.exit_code != 2 and destination.is_relative_to(bundle.resolve()):
            raise ValueError("report destination is inside the verified bundle")
        write_json_atomic(output, asdict(report))
    except (OSError, RuntimeError, ValueError):
        print("Could not write the bundle report to the requested destination.", file=sys.stderr)
        return 2

    print(f"Bundle verification {report.status}")
    return report.exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog_verifier: CatalogVerifier | None = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "verify-bundle":
        verifier = catalog_verifier if catalog_verifier is not None else PowerShellCatalogVerifier()
        return _verify_command(args.bundle, args.report, verifier)
    return 0

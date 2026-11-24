import argparse
import os
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from raglab.offline.bundle import verify_bundle
from raglab.offline.jsonio import write_json_atomic
from raglab.offline.ports import CatalogVerifier, PublisherAuthority
from raglab.offline.windows import WindowsCatalogVerifier
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
    return parser


def _check_report_destination(paths: WindowsLocalPaths, source: PinnedPath, output: Path) -> None:
    with paths.pin(output, missing_ok=True, directory_write_sharing=True) as destination:
        if destination.path.is_relative_to(source.path):
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

        def replace(temporary: Path, final: Path) -> None:
            with paths.pin(parent.path, directory_write_sharing=True) as publishing:
                if publishing.info.file_id != parent.info.file_id:
                    raise ValueError("report parent changed")
                protected.close()
                os.replace(temporary, final)
                if paths.identity(publishing.handle).file_id != parent.info.file_id:
                    raise ValueError("report parent changed")

        write_json_atomic(destination, value, replace=replace)


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


def main(
    argv: Sequence[str] | None = None,
    *,
    catalog_verifier: CatalogVerifier | None = None,
    publisher_authority: PublisherAuthority | None = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "verify-bundle":
        verifier = catalog_verifier if catalog_verifier is not None else WindowsCatalogVerifier()
        return _verify_command(args.bundle, args.report, verifier, publisher_authority)
    return 0

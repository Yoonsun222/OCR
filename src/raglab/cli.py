import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="raglab")
    parser.add_subparsers(dest="command", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    return 0

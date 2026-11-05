#!/usr/bin/env python3
"""
Shared CLI and diagnostics helpers for the AltStore pipeline scripts.
"""

import argparse
import sys
import traceback


_DEBUG = False


def set_debug(enabled: bool) -> None:
    global _DEBUG
    _DEBUG = enabled


def setup_stdio() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def warn(msg: str) -> None:
    print(f"  ⚠ {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if _DEBUG:
        print(f"    · {msg}")


def make_parser(
    description: str,
    *,
    token: bool = True,
    debug: bool = True,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if token:
        parser.add_argument(
            "--token",
            "--github-token",
            dest="token",
            default=None,
            metavar="TOKEN",
            help=(
                "GitHub token. Defaults to GITHUB_TOKEN / GH_TOKEN, then a "
                "file named .github-token in the repo root."
            ),
        )
    if debug:
        parser.add_argument(
            "--debug",
            action="store_true",
            help="print extra diagnostics (API/URL detail) as the script runs",
        )
    return parser


def run(parser: argparse.ArgumentParser, main_fn) -> int:
    setup_stdio()
    args = parser.parse_args()
    set_debug(getattr(args, "debug", False))
    try:
        return int(main_fn(args))
    except KeyboardInterrupt:
        error("interrupted")
        return 130
    except Exception:
        error("unexpected failure — full traceback below:")
        traceback.print_exc()
        return 1

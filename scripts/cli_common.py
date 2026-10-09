#!/usr/bin/env python3
"""
Shared CLI and diagnostics helpers for the AltStore pipeline scripts.

The module depends on nothing and holds no domain logic. It never imports
altstore_lib, so any script can import it without creating a cycle.
"""

import argparse
import sys
import traceback


_DEBUG = False


def set_debug(enabled: bool) -> None:
    """Turn extra diagnostics on/off for the whole process."""
    global _DEBUG
    _DEBUG = enabled


def setup_stdio() -> None:
    """Force UTF-8 output on Windows terminals that default to cp1252.

    You can call it on any platform, and more than once.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # e.g. a stream that doesn't support reconfigure


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
    """Build an ArgumentParser with the flags every script shares.

    ``--token`` and ``--github-token`` are aliases, so a workflow can use
    either spelling. argparse accepts both ``--token X`` and ``--token=X``.
    """
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
    """Parse the arguments, set up stdio and verbosity, call ``main_fn(args)``.

    An unexpected exception prints a full traceback before the function
    returns a non-zero exit code, so the CI log holds everything needed to
    diagnose it later.
    """
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

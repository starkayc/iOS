#!/usr/bin/env python3
"""
Shared CLI + diagnostics helpers for the AltStore pipeline scripts.

Kept deliberately dependency-free and free of domain logic (it never
imports altstore_lib) so any script can use it without import cycles.
"""

import argparse
import sys
import traceback


# ── Verbosity ────────────────────────────────────────────────────────────────

_DEBUG = False


def set_debug(enabled: bool) -> None:
    """Turn extra diagnostics on/off for the whole process."""
    global _DEBUG
    _DEBUG = enabled


def setup_stdio() -> None:
    """Force UTF-8 output on Windows terminals that default to cp1252.

    Safe to call on every platform and more than once.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # e.g. a stream that doesn't support reconfigure


def step(msg: str) -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def info(msg: str) -> None:
    print(f"  · {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠ {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if _DEBUG:
        print(f"    · {msg}")


# ── Argument parsing ─────────────────────────────────────────────────────────

def make_parser(
    description: str,
    *,
    token: bool = True,
    debug: bool = True,
) -> argparse.ArgumentParser:
    """Build an ArgumentParser with the flags every script shares.

    ``--token``/``--github-token`` are aliases so a workflow can pass
    either spelling; argparse accepts both ``--token X`` and ``--token=X``.
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


# ── Runner ───────────────────────────────────────────────────────────────────

def run(parser: argparse.ArgumentParser, main_fn) -> int:
    """Parse args, set up stdio/verbosity, run ``main_fn(args)``.

    Any unexpected exception is printed with a full traceback so the CI
    log carries everything needed to diagnose it later, then mapped to a
    non-zero exit code.
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

"""Prevent accidental use of a development checkout against live infrastructure."""

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mcps import cli


def is_checkout(source_file: Path) -> bool:
    source = source_file.resolve()
    return (
        source.parent.parent.name == "src"
        and (source.parents[2] / "pyproject.toml").is_file()
    )


def check_development_access(
    argv: Sequence[str], *, source_file: Path, allow_live_changes: bool
) -> None:
    # After another option, --help can be consumed as its value and execute.
    help_only = list(argv[-1:]) == ["--help"] and all(
        not arg.startswith("-") for arg in argv[:-1]
    )
    if is_checkout(source_file) and argv and not help_only and not allow_live_changes:
        raise SystemExit(
            "Refusing to run mcps from a development checkout against live infrastructure. "
            "Use the installed mcps for normal operations. For integration testing, select "
            "a disposable Podman machine/connection and separate MCPS_HOME first, then set "
            "MCPS_ALLOW_LIVE_CHANGES=1 for that test process only."
        )


def main() -> None:
    """Read process settings once and check them before dispatching any command."""
    argv = sys.argv[1:]
    check_development_access(
        argv,
        source_file=Path(__file__),
        allow_live_changes=os.environ.get("MCPS_ALLOW_LIVE_CHANGES") == "1",
    )
    cli.app(args=argv)

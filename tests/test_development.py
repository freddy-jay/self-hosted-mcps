import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import typer

from mcps import cli, development


def invoke_development_cli(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["mcps", *argv]):
        development.main()


class DevelopmentSafetyTests(unittest.TestCase):
    def test_checkout_entrypoint_blocks_commands_before_dispatch(self) -> None:
        for command in (
            ["tunnel", "bring"],
            ["restart", "bring"],
            ["rm", "bring"],
            ["init"],
            ["init", "--tags", "--help"],
            ["add", "pypi:example", "--cmd", "--help"],
        ):
            with (
                self.subTest(command=command),
                patch.dict(os.environ, {}, clear=True),
                patch.object(cli, "app") as dispatch,
            ):
                with self.assertRaisesRegex(SystemExit, "development checkout"):
                    invoke_development_cli(command)
                dispatch.assert_not_called()

    def test_explicit_development_opt_in_allows_dispatch(self) -> None:
        with (
            patch.dict(os.environ, {"MCPS_ALLOW_LIVE_CHANGES": "1"}),
            patch.object(cli, "app") as dispatch,
        ):
            invoke_development_cli(["tunnel", "safe"])
        dispatch.assert_called_once()

    def test_help_from_checkout_is_available_without_opt_in(self) -> None:
        for command in (["--help"], ["add", "--help"], ["secrets", "add", "--help"]):
            with (
                self.subTest(command=command),
                patch.dict(os.environ, {}, clear=True),
                patch.object(cli, "app") as dispatch,
            ):
                invoke_development_cli(command)
            dispatch.assert_called_once()

    def test_installed_copy_runs_without_development_opt_in(self) -> None:
        with (
            patch.object(
                development,
                "__file__",
                str(os.path.join(os.path.dirname(typer.__file__), "development.py")),
            ),
            patch.dict(os.environ, {}, clear=True),
            patch.object(cli, "app") as dispatch,
        ):
            self.assertFalse(development.is_checkout(Path(development.__file__)))
            invoke_development_cli(["restart", "safe"])
        dispatch.assert_called_once()

    def test_nonexplicit_opt_in_is_rejected(self) -> None:
        for value in ("0", "true", ""):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"MCPS_ALLOW_LIVE_CHANGES": value}),
                patch.object(cli, "app") as dispatch,
            ):
                with self.assertRaises(SystemExit):
                    invoke_development_cli(["restart", "safe"])
                dispatch.assert_not_called()

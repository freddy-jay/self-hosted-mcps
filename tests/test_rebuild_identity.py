import json
import subprocess
import tempfile
import time
import unittest
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import typer
from typer.testing import CliRunner, Result

from mcps import cli, config, detect, podman, probe, tailnet

SUFFIX = "example.ts.net"


def flat(result: Result) -> str:
    # Rich wraps messages at the terminal width, which differs in CI.
    return " ".join(result.output.split())


def commands(run: MagicMock) -> list[list[str]]:
    return [list(c.args[0]) for c in run.call_args_list]


class DestroyTests(unittest.TestCase):
    def test_rebuild_cleanup_keeps_the_login_and_the_state_volume(self) -> None:
        with patch.object(subprocess, "run") as run:
            podman.destroy("safe", keep_identity=True)
        self.assertEqual(commands(run), [["podman", "pod", "rm", "-f", "mcps-safe"]])

    def test_removal_logs_out_and_deletes_the_state_volume(self) -> None:
        with patch.object(subprocess, "run") as run:
            podman.destroy("safe")
        self.assertEqual(
            commands(run),
            [
                ["podman", "exec", "mcps-safe-ts", "tailscale", "logout"],
                ["podman", "pod", "rm", "-f", "mcps-safe"],
                ["podman", "volume", "rm", "-f", "mcps-ts-safe"],
            ],
        )


class ClaimNameTests(unittest.TestCase):
    def claim(self, start: str, seen: list[str]) -> tuple[str, list[list[str]]]:
        with (
            patch.object(subprocess, "run") as run,
            patch.object(cli, "node_dns_name", side_effect=seen),
            patch.object(time, "sleep"),
        ):
            got = cli.claim_node_name("safe", "mcps-safe-ts", start, timeout=1)
        return got, commands(run)

    def test_the_right_name_is_left_alone(self) -> None:
        got, ran = self.claim(f"mcp-safe.{SUFFIX}", [])
        self.assertEqual(got, f"mcp-safe.{SUFFIX}")
        self.assertEqual(ran, [])

    def test_a_suffixed_name_is_reclaimed_by_renaming_away_and_back(self) -> None:
        got, ran = self.claim(
            f"mcp-safe-1.{SUFFIX}",
            [f"mcp-safe-renaming.{SUFFIX}", f"mcp-safe.{SUFFIX}"],
        )
        self.assertEqual(got, f"mcp-safe.{SUFFIX}")
        self.assertEqual(
            ran,
            [
                [
                    "podman",
                    "exec",
                    "mcps-safe-ts",
                    "tailscale",
                    "set",
                    "--hostname=mcp-safe-renaming",
                ],
                [
                    "podman",
                    "exec",
                    "mcps-safe-ts",
                    "tailscale",
                    "set",
                    "--hostname=mcp-safe",
                ],
            ],
        )

    def test_a_name_another_node_still_holds_is_reported_as_it_is(self) -> None:
        taken = [f"mcp-safe-renaming.{SUFFIX}"] + [f"mcp-safe-1.{SUFFIX}"] * 50
        got, ran = self.claim(f"mcp-safe-1.{SUFFIX}", taken)
        self.assertEqual(got, f"mcp-safe-1.{SUFFIX}")
        self.assertEqual(ran[-1][-1], "--hostname=mcp-safe")


class RebuildCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.meta = patch.object(cli, "META_DIR", Path(self.temp.name) / "meta")
        self.meta.start()
        self.addCleanup(self.meta.stop)
        self.context = Path(self.temp.name) / "context"
        self.context.mkdir()

    def add(
        self,
        *flags: str,
        exists: bool,
        online: list[tuple[str, str] | None] | None = None,
        claimed: str | None = None,
        cfg: Mapping[str, object] | None = None,
    ) -> tuple[Result, MagicMock, MagicMock, list[tuple[str, ...]]]:
        answers = list(online or [(f"mcp-safe.{SUFFIX}", "100.64.0.1")])
        calls: list[tuple[str, ...]] = []

        def wait(container: str, timeout: int = 90) -> tuple[str, str]:
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if answer is None:
                raise typer.Exit(1)
            return answer

        def run(
            *args: str,
            check: bool = True,
            capture: bool = True,
            stdin: str | None = None,
        ) -> str:
            calls.append(args)
            return ""

        with ExitStack() as stack:
            for obj, attr, value in (
                (podman, "preflight", None),
                (cli, "migrate_authkey", None),
                (cli, "read_authkey", "fake-key"),
                (config, "load", {"https": True, **(cfg or {})}),
                (config, "ALLOWLIST_PATH", Path(self.temp.name) / "none"),
                (podman, "pod_exists", exists),
                (podman, "secret_get", "t" * 43),
                (
                    detect,
                    "fetch",
                    detect.Source("safe", "pypi:example", self.context),
                ),
                (detect, "detect", ("pip install example", "example")),
                (podman, "ensure_base_image", None),
                (podman, "stream", 0),
                (cli, "store_env", []),
                (podman, "secret_set", None),
                (podman, "write_serve_config", None),
                (probe, "initialize", "example"),
                (cli, "show_endpoint", None),
            ):
                if attr == "ALLOWLIST_PATH":
                    stack.enter_context(patch.object(obj, attr, value))
                else:
                    stack.enter_context(patch.object(obj, attr, return_value=value))
            stack.enter_context(patch.object(podman, "run", side_effect=run))
            stack.enter_context(patch.object(cli, "wait_online", side_effect=wait))
            destroy = stack.enter_context(patch.object(podman, "destroy"))
            claim = stack.enter_context(
                patch.object(
                    cli,
                    "claim_node_name",
                    side_effect=lambda name, container, dns, timeout=20: claimed or dns,
                )
            )
            result = CliRunner().invoke(
                cli.app, ["add", "pypi:example", "--name", "safe", *flags]
            )
        return result, destroy, claim, calls

    def test_rebuild_reuses_the_tailnet_node_instead_of_registering_a_new_one(
        self,
    ) -> None:
        cli.write_meta("safe", {"name": "safe", "public": True, "allow_cidrs": ""})
        result, destroy, _, _ = self.add("--force", exists=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=True)])

    def test_first_add_has_nothing_to_clean_up(self) -> None:
        result, destroy, _, _ = self.add(exists=False)
        self.assertEqual(result.exit_code, 0, result.output)
        destroy.assert_not_called()

    def test_failed_rebuild_keeps_the_identity_for_the_next_attempt(self) -> None:
        cli.write_meta("safe", {"name": "safe", "public": True, "allow_cidrs": ""})
        result, destroy, _, _ = self.add("--force", exists=True, online=[None])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(
            destroy.call_args_list,
            [call("safe", keep_identity=True), call("safe", keep_identity=True)],
        )

    def test_failed_first_add_removes_the_half_made_node(self) -> None:
        result, destroy, _, _ = self.add(exists=False, online=[None])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=False)])

    def test_a_reclaimed_name_restarts_the_sidecar_and_is_what_gets_saved(self) -> None:
        result, _, claim, calls = self.add(
            "--public",
            exists=False,
            online=[
                (f"mcp-safe-1.{SUFFIX}", "100.64.0.1"),
                (f"mcp-safe.{SUFFIX}", "100.64.0.1"),
            ],
            claimed=f"mcp-safe.{SUFFIX}",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        claim.assert_called_once()
        self.assertIn(("restart", "mcps-safe-ts"), calls)
        self.assertEqual(cli.read_meta("safe")["url"], f"https://mcp-safe.{SUFFIX}/mcp")
        self.assertNotIn("another node", flat(result))

    def test_a_name_clash_that_stays_is_reported_with_the_way_out(self) -> None:
        result, _, _, calls = self.add(
            "--public",
            exists=False,
            online=[(f"mcp-safe-1.{SUFFIX}", "100.64.0.1")],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(("restart", "mcps-safe-ts"), calls)
        self.assertEqual(
            cli.read_meta("safe")["url"], f"https://mcp-safe-1.{SUFFIX}/mcp"
        )
        self.assertIn("mcp-safe-1", flat(result))
        self.assertIn("mcps restart safe", flat(result))

    def test_new_public_servers_follow_the_configured_silent_default(self) -> None:
        for cfg, flags, expected in (
            ({"silent": True}, ("--public",), True),
            ({"silent": True}, ("--public", "--no-silent"), False),
            ({}, ("--public",), False),
            ({}, ("--public", "--silent"), True),
        ):
            with self.subTest(cfg=cfg, flags=flags):
                result, _, _, calls = self.add(*flags, exists=False, cfg=cfg)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(cli.read_meta("safe")["silent"], expected)
                app = next(c for c in calls if "mcps-safe-app" in c)
                self.assertEqual("MCP_SILENT=1" in app, expected)

    def test_a_saved_choice_beats_the_configured_default_on_rebuild(self) -> None:
        cli.write_meta(
            "safe",
            {"name": "safe", "public": True, "allow_cidrs": "", "silent": False},
        )
        self.add("--force", exists=True, cfg={"silent": True})
        self.assertFalse(cli.read_meta("safe")["silent"])


class RestartAndInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.meta = patch.object(cli, "META_DIR", Path(self.temp.name) / "meta")
        self.meta.start()
        self.addCleanup(self.meta.stop)

    def test_restart_takes_the_name_back_and_saves_the_stable_url(self) -> None:
        cli.write_meta(
            "safe", {"name": "safe", "url": f"https://mcp-safe-1.{SUFFIX}/mcp"}
        )
        waits = [
            (f"mcp-safe-1.{SUFFIX}", "100.64.0.1"),
            (f"mcp-safe.{SUFFIX}", "100.64.0.1"),
        ]
        with (
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run", return_value="") as run,
            patch.object(cli, "wait_online", side_effect=waits),
            patch.object(cli, "claim_node_name", return_value=f"mcp-safe.{SUFFIX}"),
            patch.object(config, "load", return_value={"https": True}),
            patch.object(podman, "secret_get", return_value=""),
            patch.object(cli, "show_endpoint"),
        ):
            result = CliRunner().invoke(cli.app, ["restart", "safe"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(call("restart", "mcps-safe-ts"), run.call_args_list)
        self.assertEqual(cli.read_meta("safe")["url"], f"https://mcp-safe.{SUFFIX}/mcp")

    def test_init_stores_the_silent_default_and_leaves_it_alone_otherwise(self) -> None:
        saved: list[dict[str, object]] = []
        for flags, before, expected in (
            (["--silent-default"], {}, True),
            (["--no-silent-default"], {"silent": True}, False),
            ([], {"silent": True}, True),
            ([], {}, False),
        ):
            with (
                self.subTest(flags=flags, before=before),
                patch.object(podman, "preflight"),
                patch.object(cli, "migrate_authkey"),
                patch.object(cli, "read_authkey", return_value="stored"),
                patch.object(
                    config,
                    "load",
                    return_value={"https": True, "silent": False, **before},
                ),
                patch.object(config, "save", side_effect=saved.append),
                patch.object(tailnet, "magic_dns_suffix", return_value=SUFFIX),
            ):
                result = CliRunner().invoke(cli.app, ["init", *flags])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(saved[-1]["silent"], expected)


class NodeNameTests(unittest.TestCase):
    def test_dns_name_is_read_from_the_sidecar_and_bad_output_is_empty(self) -> None:
        good = MagicMock(
            returncode=0,
            stdout=json.dumps({"Self": {"DNSName": f"mcp-safe.{SUFFIX}."}}),
        )
        for proc, expected in (
            (good, f"mcp-safe.{SUFFIX}"),
            (MagicMock(returncode=1, stdout=""), ""),
            (MagicMock(returncode=0, stdout="not json"), ""),
        ):
            with (
                self.subTest(expected=expected),
                patch.object(subprocess, "run", return_value=proc),
            ):
                self.assertEqual(cli.node_dns_name("mcps-safe-ts"), expected)


if __name__ == "__main__":
    unittest.main()

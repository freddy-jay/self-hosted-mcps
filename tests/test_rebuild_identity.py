import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import typer
from rich.console import Console
from typer.testing import CliRunner, Result

from mcps import cli, companions, config, detect, nodename, podman, probe, tailnet

SUFFIX = "example.ts.net"
STABLE = f"https://mcp-safe.{SUFFIX}/mcp"
SUFFIXED = f"https://mcp-safe-1.{SUFFIX}/mcp"
TAGS = "--advertise-tags=tag:mcp"


def flat(result: Result) -> str:
    # Rich wraps messages at the terminal width, which differs in CI.
    return " ".join(result.output.split())


def commands(run: MagicMock) -> list[list[str]]:
    return [list(c.args[0]) for c in run.call_args_list]


def set_hostname(hostname: str) -> list[str]:
    return [
        "podman",
        "exec",
        "mcps-safe-ts",
        "tailscale",
        "set",
        f"--hostname={hostname}",
    ]


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


class NodeNameTests(unittest.TestCase):
    def test_only_the_control_planes_numeric_suffix_counts_as_a_clash(self) -> None:
        for dns, expected in (
            (f"mcp-safe-1.{SUFFIX}", True),
            (f"mcp-safe-12.{SUFFIX}", True),
            (f"mcp-safe.{SUFFIX}", False),
            (f"tools.{SUFFIX}", False),
            (f"mcp-safer-1.{SUFFIX}", False),
            (f"mcp-safe-1a.{SUFFIX}", False),
            (f"{nodename.away_hostname('safe')}.{SUFFIX}", False),
            ("", False),
        ):
            with self.subTest(dns=dns):
                self.assertEqual(nodename.was_suffixed("safe", dns), expected)

    def test_the_throwaway_hostname_fits_a_dns_label_and_differs(self) -> None:
        for name in ("a", "safe", "x" * 51, "y" * 59, "z" * 57 + "-q"):
            with self.subTest(length=len(name)):
                away = nodename.away_hostname(name)
                self.assertLessEqual(len(away), 63)
                self.assertNotEqual(away, nodename.wanted_hostname(name))
                self.assertRegex(away, r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

    def test_dns_name_is_read_from_the_sidecar_and_bad_output_is_empty(self) -> None:
        good = json.dumps({"Self": {"DNSName": f"mcp-safe.{SUFFIX}."}})
        for code, stdout, expected in (
            (0, good, f"mcp-safe.{SUFFIX}"),
            (1, good, ""),
            (0, "not json", ""),
            (0, "[]", ""),
            (0, json.dumps({"Self": None}), ""),
            (0, json.dumps({"Self": {"DNSName": 5}}), ""),
        ):
            with (
                self.subTest(stdout=stdout[:20], code=code),
                patch.object(
                    subprocess,
                    "run",
                    return_value=MagicMock(returncode=code, stdout=stdout),
                ),
            ):
                self.assertEqual(nodename.current("mcps-safe-ts"), expected)

    def test_a_hung_status_call_is_bounded_and_reads_as_unknown(self) -> None:
        hung = subprocess.TimeoutExpired("podman", 30)
        with patch.object(subprocess, "run", side_effect=hung) as run:
            self.assertEqual(nodename.current("mcps-safe-ts"), "")
        self.assertIsNotNone(run.call_args.kwargs.get("timeout"))


class ClaimTests(unittest.TestCase):
    """The control plane acts on a rename some polls after `tailscale set`."""

    away = nodename.away_hostname("safe")

    def claim(
        self,
        seen: list[str],
        *,
        start: str = f"mcp-safe-1.{SUFFIX}",
        set_results: tuple[object, ...] = (0, 0),
    ) -> tuple[str, list[list[str]], int]:
        clock = [0.0]
        polls = iter(seen)
        last = [""]
        results = iter(set_results)
        self.seen_at_set: list[str] = []

        def current(container: str) -> str:
            last[0] = next(polls, last[0])
            return last[0]

        def run(argv: list[str], **kwargs: object) -> MagicMock:
            self.seen_at_set.append(nodename.label(last[0]))
            outcome = next(results)
            if isinstance(outcome, BaseException):
                raise outcome
            return MagicMock(returncode=outcome)

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        with (
            patch.object(subprocess, "run", side_effect=run) as ran,
            patch.object(nodename, "current", side_effect=current) as polled,
            patch.object(time, "sleep", side_effect=sleep),
            patch.object(time, "monotonic", side_effect=lambda: clock[0]),
        ):
            got = nodename.claim("safe", "mcps-safe-ts", start, timeout=10)
        return got, commands(ran), polled.call_count

    def dns(self, host: str) -> str:
        return f"{host}.{SUFFIX}"

    def test_a_name_that_was_never_suffixed_is_left_alone(self) -> None:
        for start in (self.dns("mcp-safe"), self.dns("tools"), ""):
            with self.subTest(start=start):
                got, ran, polls = self.claim([], start=start)
                self.assertEqual((got, ran, polls), (start, [], 0))

    def test_suffixed_name_is_reclaimed_once_the_control_plane_catches_up(self) -> None:
        got, ran, _ = self.claim(
            [
                self.dns("mcp-safe-1"),  # the first rename has not landed yet
                self.dns(self.away),
                self.dns(self.away),  # nor has the rename back
                self.dns("mcp-safe"),
            ]
        )
        self.assertEqual(got, self.dns("mcp-safe"))
        self.assertEqual(ran, [set_hostname(self.away), set_hostname("mcp-safe")])
        # The rename back is only sent once the control plane shows the throwaway
        # name; sent earlier, both renames can collapse into no change at all.
        self.assertEqual(self.seen_at_set, ["", self.away])

    def test_a_name_another_node_still_holds_comes_back_suffixed(self) -> None:
        got, ran, _ = self.claim(
            [self.dns(self.away), self.dns(self.away), self.dns("mcp-safe-1")]
        )
        self.assertEqual(got, self.dns("mcp-safe-1"))
        self.assertEqual(ran[-1], set_hostname("mcp-safe"))

    def test_the_real_hostname_is_requested_again_even_when_renaming_fails(
        self,
    ) -> None:
        timeout = subprocess.TimeoutExpired("podman", 30)
        for first in (1, timeout):
            with self.subTest(first=first):
                got, ran, polls = self.claim(
                    [self.dns("mcp-safe-1")], set_results=(first, 0)
                )
                self.assertEqual(got, self.dns("mcp-safe-1"))
                self.assertEqual(
                    ran, [set_hostname(self.away), set_hostname("mcp-safe")]
                )
                # No waiting for a rename that was never accepted.
                self.assertEqual(polls, 1)

    def test_waiting_is_bounded_when_the_control_plane_never_answers(self) -> None:
        got, ran, polls = self.claim([self.dns("mcp-safe-1")] * 100)
        self.assertEqual(got, self.dns("mcp-safe-1"))
        self.assertEqual(ran[-1], set_hostname("mcp-safe"))
        # Two waits of 10 simulated seconds, polled every 2.
        self.assertLessEqual(polls, 14)

    def test_a_late_answer_is_not_mistaken_for_a_name_that_is_still_taken(self) -> None:
        # The throwaway name never shows up in time, then both renames land.
        # Reporting "still suffixed" at once would save a URL about to die.
        late = [self.dns("mcp-safe-1")] * 8 + [self.dns("mcp-safe")]
        got, ran, _ = self.claim(late)
        self.assertEqual(got, self.dns("mcp-safe"))
        self.assertEqual(ran, [set_hostname(self.away), set_hostname("mcp-safe")])

    def test_a_node_left_on_the_throwaway_name_is_reported_as_it_is(self) -> None:
        got, _, _ = self.claim([self.dns(self.away)] * 100)
        self.assertEqual(got, self.dns(self.away))


class RebuildCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.meta = patch.object(cli, "META_DIR", Path(self.temp.name) / "meta")
        self.meta.start()
        self.addCleanup(self.meta.stop)
        self.context = Path(self.temp.name) / "context"
        self.context.mkdir()

    def deployed(self, **extra: object) -> None:
        cli.write_meta(
            "safe",
            {
                "name": "safe",
                "url": STABLE,
                "public": True,
                "public_url": STABLE,
                "allow_cidrs": "",
                "silent": False,
                **extra,
            },
        )

    def add(
        self,
        *flags: str,
        exists: bool,
        online: list[tuple[str, str] | None] | None = None,
        claimed: str | None = None,
        cfg: Mapping[str, object] | None = None,
        restart_fails: bool = False,
        authkey: str = "tskey-auth-first",
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
            if restart_fails and args[0] == "restart":
                raise podman.PodmanError("podman restart failed: no such container")
            return ""

        with ExitStack() as stack:
            for obj, attr, value in (
                (podman, "preflight", None),
                (cli, "migrate_authkey", None),
                (cli, "read_authkey", authkey),
                (config, "load", {"https": True, "ts_extra_args": TAGS, **(cfg or {})}),
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
                stack.enter_context(patch.object(obj, attr, return_value=value))
            stack.enter_context(
                patch.object(config, "ALLOWLIST_PATH", Path(self.temp.name) / "none")
            )
            stack.enter_context(patch.object(podman, "run", side_effect=run))
            stack.enter_context(patch.object(cli, "wait_online", side_effect=wait))
            destroy = stack.enter_context(patch.object(podman, "destroy"))
            claim = stack.enter_context(
                patch.object(
                    nodename,
                    "claim",
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
        self.deployed()
        result, destroy, _, _ = self.add("--force", exists=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=True)])
        self.assertEqual(cli.read_meta("safe")["ts_extra_args"], TAGS)
        self.assertNotIn("URL changed", flat(result))

    def test_first_add_clears_leftovers_before_it_deploys(self) -> None:
        result, destroy, _, _ = self.add(exists=False)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=False)])

    def test_identity_survives_every_failed_attempt_of_a_rebuild(self) -> None:
        self.deployed()
        first, destroy, _, _ = self.add("--force", exists=True, online=[None])
        self.assertEqual(first.exit_code, 1, first.output)
        self.assertEqual(
            destroy.call_args_list,
            [call("safe", keep_identity=True), call("safe", keep_identity=True)],
        )
        # The pod is gone now, so the retry no longer sees an existing server.
        retry, destroy, _, _ = self.add(exists=False, online=[None])
        self.assertEqual(retry.exit_code, 1, retry.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=True)])

    def test_failed_first_add_removes_the_half_made_node(self) -> None:
        result, destroy, _, _ = self.add(exists=False, online=[None])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(
            destroy.call_args_list,
            [call("safe", keep_identity=False), call("safe", keep_identity=False)],
        )

    def test_a_change_tailscale_would_remember_gets_a_fresh_node(self) -> None:
        for saved, flags, cfg in (
            ({"public": True}, ("--private",), {}),
            ({"public": False, "public_url": ""}, ("--public",), {}),
            ({"ts_extra_args": TAGS + " --ssh"}, (), {}),
            ({"ts_extra_args": ""}, (), {}),
            ({"ts_extra_args": TAGS}, (), {"ts_extra_args": TAGS + " --ssh"}),
        ):
            with self.subTest(saved=saved, flags=flags, cfg=cfg):
                self.deployed(**saved)
                result, destroy, _, _ = self.add(
                    "--force", *flags, exists=True, cfg=cfg
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(
                    destroy.call_args_list, [call("safe", keep_identity=False)]
                )

    def test_new_node_can_be_asked_for_and_a_changed_auth_key_implies_it(self) -> None:
        self.deployed()
        result, destroy, _, _ = self.add("--force", "--new-node", exists=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=False)])
        saved = cli.read_meta("safe")["authkey_fingerprint"]
        self.assertNotIn("tskey-auth-first", json.dumps(cli.read_meta("safe")))

        # Same key: the node is reused. Another key (another tailnet, or other
        # tags) must not resume the node the old key registered.
        result, destroy, _, _ = self.add("--force", exists=True)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=True)])
        self.assertEqual(cli.read_meta("safe")["authkey_fingerprint"], saved)
        result, destroy, _, _ = self.add(
            "--force", exists=True, authkey="tskey-auth-second"
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=False)])
        self.assertNotEqual(cli.read_meta("safe")["authkey_fingerprint"], saved)

    def test_a_server_built_before_flags_were_recorded_keeps_its_node(self) -> None:
        # Its metadata cannot say which flags it was started with, so they are
        # taken as unchanged rather than costing every such server its name.
        self.deployed()
        self.assertNotIn("ts_extra_args", cli.read_meta("safe"))
        result, destroy, _, _ = self.add(
            "--force", exists=True, cfg={"ts_extra_args": TAGS + " --ssh"}
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=True)])

    def test_a_reclaimed_name_restarts_the_sidecar_and_is_what_gets_saved(self) -> None:
        self.deployed(url=SUFFIXED, public_url=SUFFIXED)
        result, _, claim, calls = self.add(
            "--force",
            exists=True,
            online=[
                (f"mcp-safe-1.{SUFFIX}", "100.64.0.1"),
                (f"mcp-safe.{SUFFIX}", "100.64.0.1"),
            ],
            claimed=f"mcp-safe.{SUFFIX}",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        claim.assert_called_once()
        self.assertIn(("restart", "mcps-safe-ts"), calls)
        meta = cli.read_meta("safe")
        self.assertEqual((meta["url"], meta["public_url"]), (STABLE, STABLE))
        self.assertNotIn("another node", flat(result))
        # Clients were pointed at the old URL; say that it moved.
        self.assertIn("URL changed", flat(result))
        self.assertIn(SUFFIXED, flat(result))

    def test_a_name_clash_that_stays_is_reported_with_the_way_out(self) -> None:
        result, _, _, calls = self.add(
            "--public",
            exists=False,
            online=[(f"mcp-safe-1.{SUFFIX}", "100.64.0.1")],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(("restart", "mcps-safe-ts"), calls)
        self.assertEqual(cli.read_meta("safe")["url"], SUFFIXED)
        self.assertIn("mcp-safe-1", flat(result))
        self.assertIn("mcps restart safe", flat(result))

    def test_a_machine_renamed_by_hand_is_not_treated_as_a_clash(self) -> None:
        result, _, _, calls = self.add(
            "--public", exists=False, online=[(f"tools.{SUFFIX}", "100.64.0.1")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(("restart", "mcps-safe-ts"), calls)
        self.assertNotIn("another node", flat(result))
        self.assertEqual(cli.read_meta("safe")["url"], f"https://tools.{SUFFIX}/mcp")

    def test_a_failed_sidecar_restart_ends_cleanly_not_with_a_traceback(self) -> None:
        result, destroy, _, _ = self.add(
            "--public",
            exists=False,
            online=[(f"mcp-safe-1.{SUFFIX}", "100.64.0.1")],
            claimed=f"mcp-safe.{SUFFIX}",
            restart_fails=True,
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertIn("podman restart failed", flat(result))
        self.assertIn("mcps restart safe", flat(result))
        # Only the clean-up before the deploy: the pod that came online stays.
        self.assertEqual(destroy.call_args_list, [call("safe", keep_identity=False)])

    def test_a_rebuilt_server_that_is_online_survives_a_failed_name_reclaim(
        self,
    ) -> None:
        self.deployed()
        for failure in ("restart", "second wait"):
            with self.subTest(failure=failure):
                result, destroy, _, _ = self.add(
                    "--force",
                    exists=True,
                    online=[(f"mcp-safe-1.{SUFFIX}", "100.64.0.1"), None]
                    if failure == "second wait"
                    else [(f"mcp-safe-1.{SUFFIX}", "100.64.0.1")],
                    claimed=f"mcp-safe.{SUFFIX}",
                    restart_fails=failure == "restart",
                )
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(
                    destroy.call_args_list, [call("safe", keep_identity=True)]
                )
                self.assertIn("mcps restart safe", flat(result))

    def test_a_node_caught_mid_rename_is_not_blamed_on_a_stale_node(self) -> None:
        away = nodename.away_hostname("safe")
        result, _, _, _ = self.add(
            "--public", exists=False, online=[(f"{away}.{SUFFIX}", "100.64.0.1")]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("another node", flat(result))
        self.assertIn("still settling", flat(result))
        self.assertIn("mcps restart safe", flat(result))

    def test_new_public_servers_follow_the_configured_silent_default(self) -> None:
        for cfg, flags, expected in (
            ({"silent": True}, ("--public",), True),
            ({"silent": True}, ("--public", "--no-silent"), False),
            ({}, ("--public",), False),
            ({}, ("--public", "--silent"), True),
        ):
            with self.subTest(cfg=cfg, flags=flags):
                cli.meta_path("safe").unlink(missing_ok=True)
                result, _, _, calls = self.add(*flags, exists=False, cfg=cfg)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(cli.read_meta("safe")["silent"], expected)
                app = next(c for c in calls if "mcps-safe-app" in c)
                self.assertEqual("MCP_SILENT=1" in app, expected)

    def test_a_private_server_going_public_follows_the_default_too(self) -> None:
        # `silent: false` is saved for every server; on a private one it was
        # never a choice, so it must not override the default.
        self.deployed(public=False, public_url="", silent=False)
        result, _, _, _ = self.add(
            "--force", "--public", exists=True, cfg={"silent": True}
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(cli.read_meta("safe")["silent"])

    def test_silent_survives_a_round_trip_through_private(self) -> None:
        self.deployed(silent=True)
        result, _, _, _ = self.add("--force", "--private", exists=True)
        self.assertEqual(result.exit_code, 0, result.output)
        result, _, _, _ = self.add("--force", "--public", exists=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(cli.read_meta("safe")["silent"])

    def test_a_public_servers_saved_choice_beats_the_default_on_rebuild(self) -> None:
        self.deployed(silent=False)
        result, _, _, _ = self.add("--force", exists=True, cfg={"silent": True})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(cli.read_meta("safe")["silent"])
        self.deployed(silent=True)
        result, _, _, _ = self.add("--force", exists=True, cfg={"silent": False})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(cli.read_meta("safe")["silent"])


class RestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.meta = patch.object(cli, "META_DIR", Path(self.temp.name) / "meta")
        self.meta.start()
        self.addCleanup(self.meta.stop)

    def restart(
        self,
        meta: Mapping[str, object],
        waits: list[tuple[str, str]],
        *,
        claimed: str | None = None,
        restart_fails: bool = False,
        pod_restart_fails: bool = False,
    ) -> tuple[Result, list[tuple[str, ...]], MagicMock]:
        cli.write_meta("safe", {"name": "safe", **meta})
        calls: list[tuple[str, ...]] = []

        def run(
            *args: str,
            check: bool = True,
            capture: bool = True,
            stdin: str | None = None,
        ) -> str:
            calls.append(args)
            if restart_fails and args == ("restart", "mcps-safe-ts"):
                raise podman.PodmanError("podman restart failed: no such container")
            if pod_restart_fails and args[:2] == ("pod", "restart"):
                raise podman.PodmanError("podman pod restart failed: container stuck")
            return ""

        with (
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run", side_effect=run),
            patch.object(cli, "wait_online", side_effect=waits),
            patch.object(
                nodename,
                "claim",
                side_effect=lambda name, container, dns, timeout=20: claimed or dns,
            ),
            patch.object(config, "load", return_value={"https": True}),
            patch.object(podman, "secret_get", return_value=""),
            patch.object(cli, "show_endpoint") as shown,
        ):
            result = CliRunner().invoke(cli.app, ["restart", "safe"])
        return result, calls, shown

    def test_restart_takes_the_name_back_and_saves_the_stable_url(self) -> None:
        for public, saved_public_url, expected_public_url in (
            (True, SUFFIXED, STABLE),
            (False, "", ""),
        ):
            with self.subTest(public=public):
                result, calls, shown = self.restart(
                    {"url": SUFFIXED, "public": public, "public_url": saved_public_url},
                    [
                        (f"mcp-safe-1.{SUFFIX}", "100.64.0.1"),
                        (f"mcp-safe.{SUFFIX}", "100.64.0.1"),
                    ],
                    claimed=f"mcp-safe.{SUFFIX}",
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn(("restart", "mcps-safe-ts"), calls)
                meta = cli.read_meta("safe")
                self.assertEqual(meta["url"], STABLE)
                self.assertEqual(meta["public_url"], expected_public_url)
                # What the user is shown is the URL that now works.
                self.assertEqual(
                    shown.call_args.args[1:3], (STABLE, expected_public_url)
                )
                self.assertIn("URL changed", flat(result))

    def test_restart_repairs_a_public_url_left_behind_by_an_older_version(self) -> None:
        result, calls, shown = self.restart(
            {"url": STABLE, "public": True, "public_url": SUFFIXED},
            [(f"mcp-safe.{SUFFIX}", "100.64.0.1")],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(("restart", "mcps-safe-ts"), calls)
        self.assertEqual(cli.read_meta("safe")["public_url"], STABLE)
        self.assertEqual(shown.call_args.args[1:3], (STABLE, STABLE))
        self.assertNotIn("URL changed", flat(result))

    def test_restart_reports_a_clash_but_not_a_machine_renamed_by_hand(self) -> None:
        for host, warned in (("mcp-safe-1", True), ("tools", False)):
            with self.subTest(host=host):
                url = f"https://{host}.{SUFFIX}/mcp"
                result, calls, _ = self.restart(
                    {"url": url, "public": False}, [(f"{host}.{SUFFIX}", "100.64.0.1")]
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertNotIn(("restart", "mcps-safe-ts"), calls)
                self.assertEqual("another node" in flat(result), warned)

    def test_a_failed_sidecar_restart_ends_cleanly_not_with_a_traceback(self) -> None:
        result, _, _ = self.restart(
            {"url": SUFFIXED, "public": False},
            [(f"mcp-safe-1.{SUFFIX}", "100.64.0.1")],
            claimed=f"mcp-safe.{SUFFIX}",
            restart_fails=True,
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertIn("podman restart failed", flat(result))
        self.assertNotIn("is back", flat(result))

    def test_a_failed_pod_restart_ends_cleanly_not_with_a_traceback(self) -> None:
        result, _, _ = self.restart(
            {"url": STABLE, "public": False},
            [(f"mcp-safe.{SUFFIX}", "100.64.0.1")],
            pod_restart_fails=True,
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertIn("podman pod restart failed", flat(result))


class InitTests(unittest.TestCase):
    def init(
        self, flags: list[str], before: Mapping[str, object]
    ) -> tuple[Result, dict[str, object], MagicMock]:
        saved: list[dict[str, object]] = []
        with (
            patch.object(podman, "preflight"),
            patch.object(cli, "migrate_authkey"),
            patch.object(cli, "read_authkey", return_value="stored"),
            # Never the machine's real key: no value to store, nothing to write.
            patch.dict(os.environ),
            patch.object(podman, "secret_set") as secret_set,
            patch.object(
                config,
                "load",
                return_value={"https": True, "silent": False, **before},
            ),
            patch.object(config, "save", side_effect=saved.append),
            patch.object(tailnet, "magic_dns_suffix", return_value=SUFFIX),
        ):
            os.environ.pop("TS_AUTHKEY", None)
            result = CliRunner().invoke(cli.app, ["init", *flags])
        return result, saved[-1] if saved else {}, secret_set

    def test_init_stores_the_silent_default_and_leaves_it_alone_otherwise(self) -> None:
        for flags, before, expected in (
            (["--silent-default"], {}, True),
            (["--no-silent-default"], {"silent": True}, False),
            ([], {"silent": True}, True),
            ([], {}, False),
        ):
            with self.subTest(flags=flags, before=before):
                result, saved, secret_set = self.init(flags, before)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(saved["silent"], expected)
                secret_set.assert_not_called()

    def test_init_changes_https_only_when_asked(self) -> None:
        for flags, before, expected in (
            (["--silent-default"], {"https": False}, False),
            ([], {"https": False}, False),
            (["--https"], {"https": False}, True),
            (["--http"], {"https": True}, False),
            ([], {}, True),
        ):
            with self.subTest(flags=flags, before=before):
                result, saved, _ = self.init(flags, before)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(saved["https"], expected)


class SetupCheckAdviceTests(unittest.TestCase):
    def test_an_allowlist_is_named_when_a_silent_drop_hides_the_403(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                cli, "err", Console(file=output, color_system=None, width=200)
            ),
            patch.object(companions, "volume_names", return_value=[]),
        ):
            cli.report_broken(
                "safe", STABLE, probe.ProbeError("timed out"), True, "192.0.2.0/24"
            )
            with_allowlist = output.getvalue()
            output.truncate(0)
            output.seek(0)
            cli.report_broken("safe", STABLE, probe.ProbeError("timed out"), True, "")
            without = output.getvalue()
        self.assertIn("allowlist", with_allowlist)
        self.assertNotIn("allowlist", without)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner, Result

from mcps import cli, companions, config, detect, podman, probe

IMAGE = "ghcr.io/example/service:1.2.3"
SAVED = {
    "companion_image": IMAGE,
    "companion_volumes": ["/data"],
    "companion_env_keys": ["API_KEY"],
}


@dataclass
class AddRun:
    result: Result
    calls: list[tuple[str, ...]]
    serve: MagicMock
    destroy: MagicMock
    fetch: MagicMock
    secrets: dict[str, str]
    removed_secrets: list[str]
    events: list[str] = field(default_factory=list)

    def container(self, name: str) -> tuple[str, ...] | None:
        return next((c for c in self.calls if name in c and "-d" in c), None)

    @property
    def output(self) -> str:
        # Rich wraps messages at the terminal width, which differs in CI.
        return " ".join(self.result.output.split())


def option_pairs(args: tuple[str, ...]) -> list[tuple[str, str]]:
    return list(zip(args, args[1:], strict=False))


class CompanionSettingsTests(unittest.TestCase):
    def test_image_must_be_fully_qualified(self) -> None:
        for ref in (
            IMAGE,
            "localhost/notes-headless:1",
            "ghcr.io/example/service@sha256:" + "a" * 64,
        ):
            with self.subTest(ref=ref):
                self.assertEqual(companions.validate_image(ref), ref)
        for ref in ("redis", "example/service", "-rm", "ghcr.io/a b", "", "ghcr.io/"):
            with (
                self.subTest(ref=ref),
                self.assertRaises(companions.CompanionConfigurationError),
            ):
                companions.validate_image(ref)

    def test_mount_path_must_be_absolute_and_plain(self) -> None:
        self.assertEqual(companions.validate_mount("/data"), "/data")
        self.assertEqual(companions.validate_mount("/var/lib/app/"), "/var/lib/app")
        for path in ("data", "/", "/a/../b", "/a:ro", "/a,b", "/a b", ""):
            with (
                self.subTest(path=path),
                self.assertRaises(companions.CompanionConfigurationError),
            ):
                companions.validate_mount(path)

    def test_volume_names_cannot_collide_across_servers(self) -> None:
        self.assertEqual(
            companions.volume_name("notes", "/data"), "mcps-companion-notes--data"
        )
        self.assertNotEqual(
            companions.volume_name("a", "/b/c"), companions.volume_name("a--b", "/c")
        )
        self.assertNotEqual(
            companions.volume_name("a", "/b/c"), companions.volume_name("a-b", "/c")
        )

    def test_secret_names_cannot_collide_with_server_environment_secrets(self) -> None:
        self.assertEqual(
            companions.env_secret_name("notes", "API_KEY"),
            "mcps-notes-env-companion--api-key",
        )
        self.assertNotEqual(
            companions.env_secret_name("notes", "X"),
            podman.env_secret_name("notes", "COMPANION_X"),
        )

    def test_environment_values_must_not_be_empty(self) -> None:
        self.assertEqual(companions.parse_env(["A=1", "B=x=y"]), {"A": "1", "B": "x=y"})
        for item in ("NOVALUE", "EMPTY="):
            with (
                self.subTest(item=item),
                self.assertRaises(companions.CompanionConfigurationError),
            ):
                companions.parse_env([item])

    def test_saved_settings_round_trip_and_reject_tampering(self) -> None:
        self.assertIsNone(companions.from_meta({"name": "plain"}))
        saved = companions.from_meta(dict(SAVED))
        assert saved is not None
        self.assertEqual(saved.to_meta(), SAVED)
        for broken in (
            {"companion_image": "--privileged"},
            {"companion_image": None},
            {"companion_volumes": ["relative"]},
            {"companion_volumes": "/data"},
            {"companion_env_keys": ["BAD-NAME"]},
        ):
            with (
                self.subTest(broken=broken),
                self.assertRaises(companions.CompanionConfigurationError),
            ):
                companions.from_meta({**SAVED, **broken})

    def test_crash_looping_container_does_not_count_as_running(self) -> None:
        for samples, fresh, expected in (
            (["true 0", "true 0"], True, True),
            (["true 3", "true 3"], False, True),
            (["true 3", "true 3"], True, False),
            (["true 0", "true 1"], True, False),
            (["true 4", "true 6"], False, False),
            (["false 0", "false 0"], False, False),
            (["true 0", "false 0"], False, False),
            (["", ""], False, False),
        ):
            with (
                self.subTest(samples=samples, fresh=fresh),
                patch.object(podman, "run", side_effect=samples),
                patch.object(time, "sleep"),
            ):
                self.assertEqual(
                    companions.stays_running("notes", fresh=fresh), expected
                )

    def test_only_this_servers_volumes_are_listed(self) -> None:
        listed = "mcps-companion-a--data\nmcps-companion-a--b--data\nother\n"
        with patch.object(podman, "run", return_value=listed) as run:
            self.assertEqual(companions.volume_names("a"), ["mcps-companion-a--data"])
        self.assertIn("label=mcps.companion=a", run.call_args.args)


class CompanionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.meta = patch.object(cli, "META_DIR", Path(self.temp.name) / "meta")
        self.meta.start()
        self.addCleanup(self.meta.stop)
        self.context = Path(self.temp.name) / "context"
        self.context.mkdir()
        cli.write_meta(
            "safe",
            {
                "name": "safe",
                "url": "https://mcp-safe.example/mcp",
                "public": True,
                "public_url": "https://mcp-safe.example/mcp",
                "token_fingerprint": "existing",
                "allow_cidrs": "",
                "silent": False,
            },
        )

    def add(
        self,
        *flags: str,
        name: str = "safe",
        exists: bool = True,
        inspect: tuple[str, str] = ("true 0", "true 0"),
        image_present: bool = True,
        pull_fails: bool = False,
        online_fails: bool = False,
        handshake_fails: bool = False,
    ) -> AddRun:
        calls: list[tuple[str, ...]] = []
        events: list[str] = []
        secrets = {
            "mcps-safe-token": "t" * 43,
            "mcps-safe-env-companion--api-key": "stored",
        }
        removed: list[str] = []
        samples = list(inspect)

        def run(
            *args: str,
            check: bool = True,
            capture: bool = True,
            stdin: str | None = None,
        ) -> str:
            calls.append(args)
            if args[0] == "pull":
                events.append("pull")
                if pull_fails:
                    raise podman.PodmanError("pull failed")
            if args[:2] == ("container", "inspect"):
                return samples.pop(0) if len(samples) > 1 else samples[0]
            return ""

        def stream(*args: str) -> int:
            events.append(args[0])
            return 0

        def destroy(server: str) -> None:
            events.append("destroy")

        def online(container: str, timeout: int = 90) -> tuple[str, str]:
            if online_fails:
                raise typer.Exit(1)
            return ("mcp-safe.example", "100.64.0.1")

        def handshake(url: str, token: str) -> str:
            if handshake_fails:
                raise probe.ProbeError("no answer")
            return "example"

        with ExitStack() as stack:
            for obj, attr, value in (
                (podman, "preflight", None),
                (cli, "migrate_authkey", None),
                (cli, "read_authkey", "fake-key"),
                (config, "load", {"https": True}),
                (podman, "pod_exists", exists),
                (podman, "image_exists", image_present),
                (detect, "detect", ("pip install example", "example")),
                (podman, "ensure_base_image", None),
                (cli, "local_healthy", True),
                (cli, "show_endpoint", None),
            ):
                stack.enter_context(patch.object(obj, attr, return_value=value))
            for obj, attr, effect in (
                (podman, "stream", stream),
                (cli, "wait_online", online),
                (probe, "initialize", handshake),
                (podman, "secret_get", lambda n: secrets.get(n, "")),
                (podman, "secret_set", secrets.__setitem__),
                (podman, "secret_rm", removed.append),
                (podman, "run", run),
            ):
                stack.enter_context(patch.object(obj, attr, side_effect=effect))
            stack.enter_context(patch.object(time, "sleep"))
            stack.enter_context(
                patch.object(config, "ALLOWLIST_PATH", Path(self.temp.name) / "none")
            )
            fetch = stack.enter_context(
                patch.object(
                    detect,
                    "fetch",
                    return_value=detect.Source(name, "pypi:example", self.context),
                )
            )
            serve = stack.enter_context(patch.object(podman, "write_serve_config"))
            destroyed = stack.enter_context(
                patch.object(podman, "destroy", side_effect=destroy)
            )
            result = CliRunner().invoke(
                cli.app,
                ["add", "pypi:example", "--name", name, *flags],
            )
        return AddRun(result, calls, serve, destroyed, fetch, secrets, removed, events)

    def test_add_starts_companion_in_the_pod_with_volume_and_secret_env(self) -> None:
        ran = self.add(
            "--companion",
            IMAGE,
            "--companion-volume",
            "/data",
            "--companion-env",
            "API_KEY=s3cret-value",
            name="fresh",
            exists=False,
        )
        self.assertEqual(ran.result.exit_code, 0, ran.result.output)
        companion = ran.container("mcps-fresh-companion")
        assert companion is not None
        self.assertEqual(companion[-1], IMAGE)
        pairs = option_pairs(companion)
        for expected in (
            ("--pod", "mcps-fresh"),
            ("--restart", "always"),
            ("-v", "mcps-companion-fresh--data:/data"),
            ("--secret", "mcps-fresh-env-companion--api-key,type=env,target=API_KEY"),
        ):
            self.assertIn(expected, pairs)
        self.assertIn("--security-opt=no-new-privileges", companion)
        self.assertNotIn("-p", companion)
        self.assertNotIn("--publish", companion)
        self.assertEqual(
            ran.secrets["mcps-fresh-env-companion--api-key"], "s3cret-value"
        )
        self.assertIn(
            (
                "volume",
                "create",
                "--ignore",
                "--label",
                "mcps.companion=fresh",
                "mcps-companion-fresh--data",
            ),
            ran.calls,
        )
        meta = cli.read_meta("fresh")
        self.assertEqual(meta["companion_image"], IMAGE)
        self.assertEqual(meta["companion_volumes"], ["/data"])
        self.assertEqual(meta["companion_env_keys"], ["API_KEY"])
        self.assertNotIn(
            "s3cret-value", json.dumps(meta) + ran.result.output + repr(ran.calls)
        )

    def test_a_companion_adds_no_route_to_the_serve_config(self) -> None:
        plain = self.add("--force")
        with_companion = self.add("--force", "--companion", IMAGE)
        self.assertEqual(with_companion.result.exit_code, 0, with_companion.output)
        self.assertEqual(
            with_companion.serve.call_args.args[1], plain.serve.call_args.args[1]
        )

    def test_servers_without_a_companion_are_built_exactly_as_before(self) -> None:
        ran = self.add("--force")
        self.assertEqual(ran.result.exit_code, 0, ran.result.output)
        self.assertIsNone(ran.container("mcps-safe-companion"))
        self.assertFalse(any(c[0] == "volume" for c in ran.calls))
        self.assertEqual(
            json.loads(ran.serve.call_args.args[1])["TCP"], {"443": {"HTTPS": True}}
        )
        self.assertFalse([k for k in cli.read_meta("safe") if "companion" in k])

    def test_rebuild_without_flags_keeps_companion_and_never_removes_its_volume(
        self,
    ) -> None:
        cli.write_meta("safe", {**cli.read_meta("safe"), **SAVED})
        ran = self.add("--force")
        self.assertEqual(ran.result.exit_code, 0, ran.result.output)
        companion = ran.container("mcps-safe-companion")
        assert companion is not None
        self.assertEqual(companion[-1], IMAGE)
        self.assertIn("mcps-companion-safe--data:/data", companion)
        self.assertIn(
            "mcps-safe-env-companion--api-key,type=env,target=API_KEY", companion
        )
        self.assertFalse(any(c[:2] == ("volume", "rm") for c in ran.calls))
        self.assertEqual(ran.removed_secrets, [])
        meta = cli.read_meta("safe")
        for key, value in SAVED.items():
            self.assertEqual(meta[key], value, key)

    def test_passing_companion_again_redefines_its_volumes(self) -> None:
        cli.write_meta("safe", {**cli.read_meta("safe"), **SAVED})
        ran = self.add("--force", "--companion", "localhost/other:2")
        self.assertEqual(ran.result.exit_code, 0, ran.result.output)
        meta = cli.read_meta("safe")
        self.assertEqual(meta["companion_image"], "localhost/other:2")
        self.assertEqual(meta["companion_volumes"], [])
        self.assertEqual(meta["companion_env_keys"], ["API_KEY"])
        self.assertFalse(any(c[:2] == ("volume", "rm") for c in ran.calls))

    def test_no_companion_removes_it_and_its_secrets_but_keeps_the_data(self) -> None:
        cli.write_meta("safe", {**cli.read_meta("safe"), **SAVED})
        ran = self.add("--force", "--no-companion")
        self.assertEqual(ran.result.exit_code, 0, ran.result.output)
        self.assertIsNone(ran.container("mcps-safe-companion"))
        self.assertEqual(ran.removed_secrets, ["mcps-safe-env-companion--api-key"])
        self.assertFalse(any(c[:2] == ("volume", "rm") for c in ran.calls))
        self.assertFalse([k for k in cli.read_meta("safe") if "companion" in k])
        self.assertTrue(cli.read_meta("safe")["public"])

    def test_failed_rebuild_with_no_companion_keeps_its_secrets(self) -> None:
        saved = {**cli.read_meta("safe"), **SAVED}
        cli.write_meta("safe", saved)
        ran = self.add("--force", "--no-companion", online_fails=True)
        self.assertNotEqual(ran.result.exit_code, 0)
        self.assertEqual(ran.removed_secrets, [])
        self.assertEqual(cli.read_meta("safe"), saved)

    def test_invalid_companion_settings_stop_before_fetch_or_destroy(self) -> None:
        for flags in (
            ("--companion", "redis"),
            ("--companion", IMAGE, "--companion-volume", "relative"),
            ("--companion", IMAGE, "--companion-env", "NOVALUE"),
            ("--companion", IMAGE, "--companion-env", "EMPTY="),
            ("--companion", IMAGE, "--companion-env", "BAD-NAME=x"),
            ("--companion", IMAGE, "--no-companion"),
            ("--companion-volume", "/data"),
            ("--companion-env", "API_KEY=x"),
        ):
            with self.subTest(flags=flags):
                ran = self.add("--force", *flags)
                self.assertEqual(ran.result.exit_code, 1, ran.result.output)
                self.assertNotIn("Traceback", ran.result.output)
                ran.fetch.assert_not_called()
                ran.destroy.assert_not_called()
                self.assertEqual(ran.secrets["mcps-safe-token"], "t" * 43)

    def test_invalid_saved_companion_stops_a_plain_rebuild_before_fetch(self) -> None:
        cli.write_meta(
            "safe",
            {**cli.read_meta("safe"), **SAVED, "companion_image": "--privileged"},
        )
        ran = self.add("--force")
        self.assertEqual(ran.result.exit_code, 1, ran.result.output)
        ran.fetch.assert_not_called()
        ran.destroy.assert_not_called()

    def test_invalid_saved_companion_can_be_replaced_or_removed(self) -> None:
        broken = {**cli.read_meta("safe"), **SAVED, "companion_volumes": "/data"}
        for flags, image in (
            (("--no-companion",), None),
            (("--companion", IMAGE), IMAGE),
        ):
            with self.subTest(flags=flags):
                cli.write_meta("safe", broken)
                ran = self.add("--force", *flags)
                self.assertEqual(ran.result.exit_code, 0, ran.result.output)
                self.assertEqual(cli.read_meta("safe").get("companion_image"), image)

    def test_missing_image_is_pulled_before_anything_is_built_or_replaced(self) -> None:
        ran = self.add("--force", "--companion", IMAGE, image_present=False)
        self.assertEqual(ran.result.exit_code, 0, ran.result.output)
        self.assertEqual(ran.events, ["pull", "build", "destroy"])
        self.assertIn(("pull", IMAGE), ran.calls)

    def test_failed_pull_leaves_the_working_pod_and_its_image_untouched(self) -> None:
        before = cli.read_meta("safe")
        ran = self.add(
            "--force", "--companion", IMAGE, image_present=False, pull_fails=True
        )
        self.assertEqual(ran.result.exit_code, 1, ran.result.output)
        self.assertEqual(ran.events, ["pull"])
        ran.fetch.assert_not_called()
        self.assertEqual(cli.read_meta("safe"), before)

    def test_companion_that_exited_is_reported_instead_of_live(self) -> None:
        ran = self.add("--force", "--companion", IMAGE, inspect=("false 0", "false 0"))
        self.assertEqual(ran.result.exit_code, 1, ran.result.output)
        self.assertIn("mcps logs safe --companion", ran.output)
        self.assertNotIn("is live", ran.output)
        self.assertEqual(ran.events, ["build", "destroy"])

    def test_crash_looping_companion_is_reported_instead_of_live(self) -> None:
        ran = self.add("--force", "--companion", IMAGE, inspect=("true 1", "true 2"))
        self.assertEqual(ran.result.exit_code, 1, ran.result.output)
        self.assertIn("mcps logs safe --companion", ran.output)
        self.assertNotIn("is live", ran.output)

    def test_failed_handshake_points_at_a_companion_that_is_down(self) -> None:
        ran = self.add(
            "--force",
            "--companion",
            IMAGE,
            handshake_fails=True,
            inspect=("false 0", "false 0"),
        )
        self.assertEqual(ran.result.exit_code, 1, ran.result.output)
        self.assertIn("mcps logs safe --companion", ran.output)

    def test_failed_handshake_warns_before_suggesting_rm_with_companion_data(
        self,
    ) -> None:
        with patch.object(
            companions, "volume_names", return_value=["mcps-companion-safe--data"]
        ):
            ran = self.add(
                "--force",
                "--companion",
                IMAGE,
                "--companion-volume",
                "/data",
                handshake_fails=True,
            )
        self.assertEqual(ran.result.exit_code, 1, ran.result.output)
        self.assertIn("--keep-data", ran.output)

    def rm(
        self, *flags: str, volumes: str, known: bool = True
    ) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []

        def run(
            *args: str,
            check: bool = True,
            capture: bool = True,
            stdin: str | None = None,
        ) -> str:
            calls.append(args)
            return volumes if args[:2] == ("volume", "ls") else ""

        if not known:
            cli.meta_path("safe").unlink()
        with (
            patch.object(podman, "pod_exists", return_value=known),
            patch.object(podman, "destroy") as destroy,
            patch.object(podman, "run", side_effect=run),
            patch.object(podman, "secret_get", return_value=""),
            patch.object(podman, "secret_rm"),
            patch.object(podman, "secret_names", return_value=[]),
            patch.object(config, "SRC_DIR", Path(self.temp.name) / "src"),
        ):
            result = CliRunner().invoke(cli.app, ["rm", "safe", *flags])
        self.assertEqual(result.exit_code, 0, result.output)
        destroy.assert_called_once_with("safe")
        return calls

    def test_rm_deletes_companion_volumes_found_by_label(self) -> None:
        calls = self.rm(
            volumes="mcps-companion-safe--data\nmcps-companion-safe--config\n"
        )
        self.assertIn(
            (
                "volume",
                "ls",
                "--filter",
                "label=mcps.companion=safe",
                "--format",
                "{{.Name}}",
            ),
            calls,
        )
        self.assertIn(("volume", "rm", "-f", "mcps-companion-safe--data"), calls)
        self.assertIn(("volume", "rm", "-f", "mcps-companion-safe--config"), calls)

    def test_rm_keep_data_leaves_companion_volumes(self) -> None:
        calls = self.rm("--keep-data", volumes="mcps-companion-safe--data\n")
        self.assertFalse(any(c[:2] == ("volume", "rm") for c in calls))

    def test_rm_cleans_up_a_volume_left_by_a_failed_first_add(self) -> None:
        calls = self.rm(volumes="mcps-companion-safe--data\n", known=False)
        self.assertIn(("volume", "rm", "-f", "mcps-companion-safe--data"), calls)

    def test_logs_can_show_the_companion(self) -> None:
        with (
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "stream", return_value=0) as stream,
        ):
            result = CliRunner().invoke(cli.app, ["logs", "safe", "--companion"])
            both = CliRunner().invoke(
                cli.app, ["logs", "safe", "--companion", "--tunnel"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        stream.assert_called_once_with("logs", "--tail", "200", "mcps-safe-companion")
        self.assertEqual(both.exit_code, 1, both.output)

    def test_restart_does_not_report_success_for_exited_companion(self) -> None:
        cli.write_meta("safe", {**cli.read_meta("safe"), **SAVED})
        with (
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run", return_value="false 0"),
            patch.object(time, "sleep"),
            patch.object(
                cli, "wait_online", return_value=("mcp-safe.example", "100.64.0.1")
            ),
            patch.object(config, "load", return_value={"https": True}),
            patch.object(podman, "secret_get", return_value=""),
        ):
            result = CliRunner().invoke(cli.app, ["restart", "safe"])
        output = " ".join(result.output.split())
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("mcps logs safe --companion", output)
        self.assertNotIn("is back", output)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from mcps import cli, config, detect, podman, probe, tunnels


class TunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.meta = patch.object(cli, "META_DIR", Path(self.temp.name))
        self.meta.start()
        self.addCleanup(self.meta.stop)
        cli.write_meta(
            "safe",
            {
                "name": "safe",
                "url": "https://mcp-safe.example/mcp",
                "public": True,
                "public_url": "https://mcp-safe.example/mcp",
                "token_fingerprint": "existing",
                "allow_cidrs": "192.0.2.0/24",
                "silent": True,
            },
        )

    def test_invalid_tunnel_id_cannot_change_existing_server(self) -> None:
        with (
            patch.object(podman, "preflight"),
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run") as run,
        ):
            result = CliRunner().invoke(
                cli.app,
                ["tunnel", "safe", "--tunnel-id", "../invalid", "--key-stdin"],
                input="test-secret\n",
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("tunnel ID", result.output)
        run.assert_not_called()
        self.assertTrue(cli.read_meta("safe")["public"])

    def test_missing_key_leaves_public_server_untouched(self) -> None:
        with (
            patch.object(podman, "preflight"),
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "secret_get", return_value=""),
            patch.object(podman, "run") as run,
        ):
            result = CliRunner().invoke(
                cli.app,
                ["tunnel", "safe", "--tunnel-id", "tunnel_" + "a" * 32, "--key-stdin"],
                input="",
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("runtime API key", result.output)
        run.assert_not_called()
        self.assertTrue(cli.read_meta("safe")["public"])

    def test_readiness_requires_recent_successful_openai_poll(self) -> None:
        self.assertFalse(tunnels.poll_is_recent("# no polls yet\n", now=1000))
        self.assertFalse(
            tunnels.poll_is_recent(
                "commands_poll_last_successful_timestamp_seconds 0\n", now=1000
            )
        )
        self.assertFalse(
            tunnels.poll_is_recent(
                "commands_poll_last_successful_timestamp_seconds 100\n", now=1000
            )
        )
        self.assertTrue(
            tunnels.poll_is_recent(
                'commands_poll_last_successful_timestamp_seconds{channel="main"} 990\n',
                now=1000,
            )
        )

    def test_failed_readiness_does_not_disable_working_public_endpoint(self) -> None:
        with (
            patch.object(podman, "preflight"),
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(tunnels, "pull_image", return_value="digest"),
            patch.object(podman, "secret_set"),
            patch.object(tunnels, "start"),
            patch.object(tunnels, "wait_ready", return_value=False),
            patch.object(podman, "write_serve_config") as serve,
        ):
            result = CliRunner().invoke(
                cli.app,
                ["tunnel", "safe", "--tunnel-id", "tunnel_" + "a" * 32, "--key-stdin"],
                input="test-secret\n",
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(cli.read_meta("safe")["public"])
        serve.assert_not_called()

    def test_remove_cannot_reenable_public_access(self) -> None:
        meta = cli.read_meta("safe")
        meta.update(public=False, tunnel_id="tunnel_" + "a" * 32, tunnel_image="digest")
        cli.write_meta("safe", meta)
        with (
            patch.object(podman, "preflight"),
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run"),
            patch.object(podman, "secret_rm"),
        ):
            result = CliRunner().invoke(cli.app, ["tunnel", "safe", "--remove"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(cli.read_meta("safe")["public"])
        self.assertNotIn("tunnel_id", cli.read_meta("safe"))

    def test_setup_preserves_public_access_and_keeps_key_out_of_metadata(self) -> None:
        previous = cli.read_meta("safe")
        calls: list[tuple[str, ...]] = []

        def run(
            *args: str,
            check: bool = True,
            capture: bool = True,
            stdin: str | None = None,
        ) -> str:
            calls.append(args)
            if args[:2] == ("image", "inspect"):
                return "ghcr.io/openai/tunnel-client@sha256:" + "a" * 64
            return ""

        with (
            patch.object(podman, "preflight"),
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run", side_effect=run),
            patch.object(podman, "secret_set") as secret,
            patch.object(config, "load", return_value={"https": False}),
            patch.object(podman, "write_serve_config") as serve,
            patch.object(tunnels, "wait_ready", return_value=True),
        ):
            result = CliRunner().invoke(
                cli.app,
                ["tunnel", "safe", "--tunnel-id", "tunnel_" + "a" * 32, "--key-stdin"],
                input="test-secret\n",
            )
        self.assertEqual(result.exit_code, 0, result.output)
        meta = cli.read_meta("safe")
        for key, value in previous.items():
            self.assertEqual(meta[key], value, key)
        self.assertEqual(meta["tunnel_id"], "tunnel_" + "a" * 32)
        self.assertNotIn("test-secret", json.dumps(meta) + result.output + repr(calls))
        secret.assert_called_once_with("mcps-safe-openai-key", "test-secret")
        sidecar = next(c for c in calls if "mcps-safe-tunnel" in c and "-d" in c)
        self.assertIn("MCP_SERVER_URL=http://127.0.0.1:8081/mcp", sidecar)
        self.assertIn("HEALTH_LISTEN_ADDR=127.0.0.1:8082", sidecar)
        self.assertIn(
            "mcps-safe-openai-key,type=env,target=CONTROL_PLANE_API_KEY", sidecar
        )
        self.assertNotIn("-p", sidecar)
        serve.assert_not_called()
        self.assertFalse(any(c[0] == "restart" for c in calls))

    def test_setup_preserves_private_access(self) -> None:
        previous = {
            "name": "safe",
            "url": "http://mcp-safe.example/mcp",
            "public": False,
        }
        cli.write_meta("safe", previous)
        with (
            patch.object(podman, "preflight"),
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(tunnels, "pull_image", return_value="digest"),
            patch.object(podman, "secret_set"),
            patch.object(tunnels, "start"),
            patch.object(tunnels, "wait_ready", return_value=True),
            patch.object(podman, "write_serve_config") as serve,
            patch.object(podman, "run") as run,
        ):
            result = CliRunner().invoke(
                cli.app,
                ["tunnel", "safe", "--tunnel-id", "tunnel_" + "a" * 32, "--key-stdin"],
                input="test-secret\n",
            )
        self.assertEqual(result.exit_code, 0, result.output)
        for key, value in previous.items():
            self.assertEqual(cli.read_meta("safe")[key], value, key)
        serve.assert_not_called()
        run.assert_not_called()

    def test_removing_tunnel_preserves_public_access(self) -> None:
        previous = cli.read_meta("safe")
        cli.write_meta(
            "safe",
            dict(previous, tunnel_id="tunnel_" + "a" * 32, tunnel_image="digest"),
        )
        with (
            patch.object(podman, "preflight"),
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run") as run,
            patch.object(podman, "secret_rm") as secret,
            patch.object(podman, "write_serve_config") as serve,
        ):
            result = CliRunner().invoke(cli.app, ["tunnel", "safe", "--remove"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(cli.read_meta("safe"), previous)
        run.assert_called_once_with("rm", "-f", "--ignore", "mcps-safe-tunnel")
        secret.assert_called_once_with("mcps-safe-openai-key")
        serve.assert_not_called()

    def test_restart_does_not_report_success_for_broken_tunnel(self) -> None:
        meta = cli.read_meta("safe")
        meta.update(public=False, tunnel_id="tunnel_" + "a" * 32)
        cli.write_meta("safe", meta)
        with (
            patch.object(podman, "pod_exists", return_value=True),
            patch.object(podman, "run"),
            patch.object(
                cli, "wait_online", return_value=("mcp-safe.example", "100.64.0.1")
            ),
            patch.object(config, "load", return_value={"https": True}),
            patch.object(podman, "secret_get", return_value=""),
            patch.object(tunnels, "wait_ready", return_value=False),
        ):
            result = CliRunner().invoke(cli.app, ["restart", "safe"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("tunnel", result.output)
        self.assertNotIn("is back", result.output)

    def test_rebuild_preserves_public_access_and_tunnel(self) -> None:
        previous = cli.read_meta("safe")
        previous.update(tunnel_id="tunnel_" + "a" * 32, tunnel_image="digest")
        cli.write_meta("safe", previous)
        for flags, public, policy, silent in (
            ([], True, previous["allow_cidrs"], True),
            (["--public"], True, previous["allow_cidrs"], True),
            (["--private"], False, "", True),
            (["--allow", "any", "--no-silent"], True, "", False),
        ):
            with self.subTest(flags=flags):
                cli.write_meta("safe", previous)
                with ExitStack() as stack:
                    for obj, attr, value in (
                        (podman, "preflight", None),
                        (cli, "migrate_authkey", None),
                        (cli, "read_authkey", "fake-key"),
                        (config, "load", {"https": True}),
                        (podman, "pod_exists", True),
                        (podman, "secret_get", "t" * 43),
                        (
                            detect,
                            "fetch",
                            detect.Source("safe", "pypi:example", Path(self.temp.name)),
                        ),
                        (detect, "detect", ("pip install example", "example")),
                        (podman, "ensure_base_image", None),
                        (podman, "stream", 0),
                        (podman, "destroy", None),
                        (cli, "store_env", []),
                        (podman, "secret_set", None),
                        (tunnels, "wait_ready", True),
                        (cli, "wait_online", ("mcp-safe.example", "100.64.0.1")),
                        (probe, "initialize", "example"),
                        (cli, "show_endpoint", None),
                    ):
                        stack.enter_context(patch.object(obj, attr, return_value=value))
                    serve = stack.enter_context(
                        patch.object(podman, "write_serve_config")
                    )
                    run = stack.enter_context(patch.object(podman, "run"))
                    start = stack.enter_context(patch.object(tunnels, "start"))
                    result = CliRunner().invoke(
                        cli.app,
                        ["add", "pypi:example", "--name", "safe", "--force", *flags],
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                meta = cli.read_meta("safe")
                self.assertEqual(meta["public"], public)
                self.assertEqual(meta["allow_cidrs"], policy)
                self.assertEqual(meta["silent"], silent)
                self.assertEqual(meta["tunnel_id"], previous["tunnel_id"])
                self.assertEqual(
                    meta["public_url"], previous["public_url"] if public else ""
                )
                self.assertEqual(
                    meta["token_fingerprint"],
                    cli.token_fingerprint("t" * 43) if public else "",
                )
                self.assertEqual(
                    next(
                        iter(
                            json.loads(serve.call_args.args[1])["AllowFunnel"].values()
                        )
                    ),
                    public,
                )
                start.assert_called_once_with(
                    "safe",
                    tunnel_id=previous["tunnel_id"],
                    image=previous["tunnel_image"],
                )
                app = next(
                    c.args for c in run.call_args_list if "mcps-safe-app" in c.args
                )
                self.assertEqual("MCP_REQUIRE_TOKEN=1" in app, public)
                self.assertEqual("MCP_SILENT=1" in app, silent)
                self.assertEqual("MCP_ALLOW_CIDRS=192.0.2.0/24" in app, bool(policy))


if __name__ == "__main__":
    unittest.main()

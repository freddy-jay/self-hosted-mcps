import json
import shlex
import tempfile
import tomllib
import unittest
import typer
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner
from mcps import cli, detect, podman, validation


class SecurityTests(unittest.TestCase):
    def test_reserved_env_namespace_cannot_collide_between_servers(self):
        for name in ('a-env-b', 'a-env', 'env'):
            with self.assertRaises(ValueError):
                podman.secret_name(name, 'token')

    def test_legacy_environment_secret_ids_stay_stable(self):
        self.assertEqual(podman.env_secret_name('safe', 'FOO__BAR'), 'mcps-safe-env-foo-bar')
        self.assertEqual(podman.env_secret_name('safe', '_TOKEN'), 'mcps-safe-env-token')
        self.assertEqual(podman.env_secret_name('safe', 'lowercase'), 'mcps-safe-env-lowercase')

    def test_colliding_environment_names_fail_before_storing(self):
        with patch.object(podman, 'secret_set') as write:
            with self.assertRaises(typer.Exit):
                cli.store_env('safe', {'FOO__BAR': 'secret'}, {'env_keys': ['FOO_BAR']})
            write.assert_not_called()

    def test_generated_build_file_cannot_follow_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'repo'
            root.mkdir()
            victim = Path(temp) / 'outside'
            victim.write_text('untouched')
            try:
                (root / 'Containerfile.mcps').symlink_to(victim)
            except OSError:
                self.skipTest('symlink creation requires host permission')
            with self.assertRaises(ValueError):
                validation.build_file(root)
            self.assertEqual(victim.read_text(), 'untouched')

    def test_subdir_cannot_escape_or_inject_containerfile(self):
        for value in ('../other', '/root', 'a/../../other', 'a\nRUN evil', 'C:\\temp'):
            with self.assertRaises(ValueError):
                validation.workdir(value)

    def test_failed_base_build_aborts(self):
        with patch.object(podman, 'image_exists', return_value=False), patch.object(podman, 'stream', return_value=1):
            with self.assertRaises(podman.PodmanError):
                podman.ensure_base_image()

    def test_names_cannot_escape_state_directory(self):
        for name in ('../escape', '..', 'a/b', 'a\\b', '-option', 'C:\\temp'):
            with self.subTest(name=name), self.assertRaises((ValueError, SystemExit)):
                cli.meta_path(name)

    def test_environment_files_cannot_override_gateway(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / 'input.env'
            env.write_text('MCP_SILENT=1\n')
            with patch.object(podman, 'preflight'), patch.object(cli, 'read_authkey', return_value='fake'), \
                 patch.object(cli.config, 'load', return_value={'https': True}), \
                 patch.object(podman, 'pod_exists', return_value=True), \
                 patch.object(podman, 'destroy') as destroy, patch.object(detect, 'fetch', side_effect=AssertionError('must reject before fetch')) as fetch:
                result = CliRunner().invoke(cli.app, ['add', 'owner/repo', '--name', 'safe', '--force', '--env-file', str(env)])
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn('MCP_SILENT', result.output)
                destroy.assert_not_called()
                fetch.assert_not_called()

    def test_package_arguments_are_not_shell_code(self):
        for origin in ('npm:pkg; echo injected', 'pypi:pkg; echo injected'):
            install, command = detect.detect(detect.Source('safe', origin, Path('.')))
            self.assertEqual(shlex.split(command)[-1], 'pkg; echo injected')
            self.assertEqual(shlex.split(install)[-1], 'pkg; echo injected')

    def test_missing_public_secret_does_not_disable_auth(self):
        with patch.object(podman, 'secret_get', return_value=''):
            args = cli.app_args('safe', [], True, '')
            self.assertIn('MCP_REQUIRE_TOKEN=1', args)

    def test_rotation_recreates_app_before_reporting_success(self):
        with patch.object(podman, 'pod_exists', return_value=True), \
             patch.object(podman, 'secret_set'), patch.object(cli, 'read_meta', return_value={'public': True}), \
             patch.object(cli, 'write_meta'), patch.object(cli, 'restart_app') as restart:
            result = CliRunner().invoke(cli.app, ['token', 'safe', '--rotate'])
            self.assertEqual(result.exit_code, 0, result.output)
            restart.assert_called_once_with('safe')

    def test_client_codex_outputs_parseable_config_without_reading_secrets(self):
        with patch.object(cli, 'require_server', return_value={'url': 'https://mcp-safe.example/mcp', 'public': True}), \
             patch.object(podman, 'secret_get', side_effect=AssertionError('must not read secrets')):
            result = CliRunner().invoke(cli.app, ['client', 'safe', '--client', 'codex'])
            self.assertEqual(result.exit_code, 0, result.output)
            config = tomllib.loads(result.output)['mcp_servers']['safe']
            self.assertEqual(config['url'], 'https://mcp-safe.example/mcp')
            self.assertEqual(config['bearer_token_env_var'], 'MCPS_SAFE_TOKEN')

    def test_openai_example_uses_environment_auth_and_approvals(self):
        with patch.object(cli, 'require_server', return_value={'url': 'https://mcp-safe.example/mcp', 'public': True}):
            result = CliRunner().invoke(cli.app, ['client', 'safe', '--client', 'openai'])
            self.assertEqual(result.exit_code, 0, result.output)
            compile(result.output, '<example>', 'exec')
            self.assertIn('os.environ["MCPS_SAFE_TOKEN"]', result.output)
            self.assertIn('"require_approval": "always"', result.output)

    def test_openai_refuses_private_endpoint(self):
        with patch.object(cli, 'require_server', return_value={'url': 'https://mcp-safe.example/mcp', 'public': False}):
            result = CliRunner().invoke(cli.app, ['client', 'safe', '--client', 'openai'])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn('--public', result.output)

if __name__ == '__main__':
    unittest.main()

import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from mcps import cli, detect, validation


class UsabilityTests(unittest.TestCase):
    def test_familiar_relative_paths_are_normalized(self):
        for value in ('./src/server', 'src/server/', '.\\src\\server', 'src\\server'):
            with self.subTest(value=value):
                self.assertEqual(validation.workdir(value), 'src/server')

    def test_normalizing_paths_does_not_allow_escape(self):
        for value in ('..\\other', '.\\..\\other', '\\root', 'C:\\temp', '/root', 'src/../other'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validation.workdir(value)

    def test_generated_names_work_without_a_manual_override(self):
        for target in ('owner/env', 'owner/mcp-env-server', 'owner/con', 'owner/' + 'a' * 80):
            with self.subTest(target=target):
                name = detect.default_name(target)
                self.assertEqual(validation.server_name(name), name)

    def test_invalid_server_names_get_cli_errors_before_podman(self):
        for command in (['rm', '../oops'], ['logs', '../oops'], ['client', '../oops'],
                        ['restart', '../oops'], ['token', '../oops'], ['secrets', 'ls', '../oops']):
            with self.subTest(command=command), patch.object(cli.podman, 'pod_exists') as exists:
                result = CliRunner().invoke(cli.app, command)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn('Invalid value', result.output)
                self.assertNotIn('Traceback', result.output)
                exists.assert_not_called()

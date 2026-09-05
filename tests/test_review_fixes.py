import io
import socket
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner
from mcps import cli, podman, probe


class ReviewFixTests(unittest.TestCase):
    def test_allowlist_probe_failure_has_targeted_advice(self):
        output = io.StringIO()
        with patch.object(cli, 'err', Console(file=output, color_system=None)):
            cli.report_broken('safe', 'https://example/mcp', probe.ProbeError('HTTP 403 Forbidden', status_code=403), True, '192.0.2.0/24')
        self.assertIn('allowlist', output.getvalue())
        self.assertNotIn('certificates', output.getvalue())

    def test_logout_timeout_does_not_prevent_removal(self):
        with patch.object(subprocess, 'run', side_effect=[subprocess.TimeoutExpired('logout', 30), None, None]) as run:
            podman.destroy('safe')
        self.assertEqual(run.call_args_list[1].args[0], ['podman', 'pod', 'rm', '-f', 'mcps-safe'])
        self.assertEqual(run.call_args_list[2].args[0], ['podman', 'volume', 'rm', '-f', 'mcps-ts-safe'])

    def test_dns_failure_is_typed_on_all_platforms(self):
        for message in ('getaddrinfo failed', 'Name or service not known', 'nodename nor servname provided'):
            with patch.object(probe, '_open', side_effect=urllib.error.URLError(socket.gaierror(-2, message))):
                with self.assertRaises(probe.ProbeError) as raised:
                    probe.initialize('https://example/mcp', attempts=1)
                self.assertTrue(raised.exception.dns_failure)

    def test_private_server_does_not_print_old_public_token(self):
        with patch.object(podman, 'pod_exists', return_value=True), patch.object(cli, 'read_meta', return_value={'public': False}), \
             patch.object(podman, 'secret_get', return_value='old-secret'):
            result = CliRunner().invoke(cli.app, ['token', 'safe'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn('old-secret', result.output)

    def test_allowlist_file_normalizes_masks_and_accepts_comments(self):
        from mcps import access
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'allowlist.txt'
            path.write_text('# optional\n192.0.2.7/255.255.255.0 # office\n2001:db8::1/64\n')
            self.assertEqual(access.resolve([], path), '192.0.2.0/24,2001:db8::/64')

    def test_allowlist_invalid_file_fails_closed_and_any_is_explicit(self):
        from mcps import access
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'allowlist.txt'
            path.write_text('192.0.2.0/24\nbad-input\n')
            with self.assertRaises(ValueError):
                access.resolve([], path)
            self.assertEqual(access.resolve(['any'], path), '')
            with self.assertRaises(ValueError):
                access.resolve(['any', '192.0.2.0/24'], None)
            with self.assertRaises(OSError):
                access.resolve([], Path(temp) / 'missing')

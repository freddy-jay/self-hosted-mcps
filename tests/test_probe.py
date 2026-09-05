import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from mcps import probe


class ProbeTests(unittest.TestCase):
    def test_redirect_does_not_forward_authorization(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                self.send_response(302)
                self.send_header('Location', '/redirected')
                self.end_headers()
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(probe.ProbeError):
                probe.initialize(f'http://127.0.0.1:{server.server_port}/mcp', 'test-only', attempts=1)
            self.assertEqual(received, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_malformed_initialize_reply_is_not_reported_as_healthy(self):
        for payload in ('{}', '[]', '{"result":{}}'):
            with self.subTest(payload=payload), self.assertRaises(probe.ProbeError):
                probe._parse(payload)

    def test_missing_tools_result_is_not_reported_as_healthy(self):
        with patch.object(probe, '_post', side_effect=[io.BytesIO(), io.BytesIO(b'{}')]):
            with self.assertRaises(probe.ProbeError):
                probe._list_tools('http://localhost/mcp', {}, '')

    def test_initialize_accepts_json_and_sse(self):
        reply = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2025-06-18',
            'capabilities': {}, 'serverInfo': {'name': 'fixture', 'version': '1'}}})
        self.assertEqual(probe._parse(reply), 'fixture 1')
        self.assertEqual(probe._parse('event: message\ndata: ' + reply + '\n\n'), 'fixture 1')

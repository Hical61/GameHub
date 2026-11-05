"""管理工具的凭据隔离、地址约束、分页及维护预算回归。"""

import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import time
import unittest
import uuid
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hub_admin", ROOT / "scripts/hub_admin.py")
ADMIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADMIN)


class FakeClient:
    """记录请求，不访问已有服务。"""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def request(self, *args):
        """保存参数，返回预设响应。"""
        self.calls.append(args)
        return next(self.replies)


class AdminTests(unittest.TestCase):
    def test_http_only_accepts_explicit_private_addresses(self):
        for value in ("http://127.0.0.1:8443", "http://192.168.1.2", "http://[::1]:8443", "https://admin.example.invalid"):
            self.assertEqual(ADMIN.validate_base_url(value), value)
        for value in ("http://example.invalid", "http://8.8.8.8", "http://169.254.1.1", "http://0.0.0.0",
                      "https://user:${SYNTHETIC_PASSWORD}@example.invalid", "https://example.invalid/admin", "https://example.invalid/?token=x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ADMIN.validate_base_url(value)

    def test_token_is_read_from_environment_and_newlines_are_rejected(self):
        with patch.dict(os.environ, {"TEST_GAMEHUB_TOKEN": "synthetic.jwt.value"}):
            self.assertEqual(ADMIN.load_token("TEST_GAMEHUB_TOKEN"), "synthetic.jwt.value")
        with patch.dict(os.environ, {"TEST_GAMEHUB_TOKEN": "jwt\r\nInjected: header"}):
            with self.assertRaises(ValueError):
                ADMIN.load_token("TEST_GAMEHUB_TOKEN")

    def test_output_cannot_alias_token_file(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        directory = ROOT / ".local" / ("admin-test-" + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(lambda: shutil.rmtree(directory))
        token = directory / "token"
        token.write_text("synthetic.jwt.value", encoding="utf-8")
        alias = directory / "output"
        os.link(token, alias)
        with self.assertRaises(ValueError):
            ADMIN.validate_output(alias, (token,))

    def test_archive_stops_when_both_stages_are_empty(self):
        args = ADMIN.parser().parse_args(["--url", "http://127.0.0.1", "archive", "--game", "demo",
                                          "--keep-days", "30", "--max-batches", "100"])
        client = FakeClient([{"archivedTasks": 1, "redactedEvents": 0}, {"archivedTasks": 0, "redactedEvents": 0}])
        result = ADMIN.execute(client, args)
        self.assertEqual(result, {"batches": 2, "archivedTasks": 1, "redactedEvents": 0, "exhausted": True})

    def test_archive_does_not_exceed_batch_cap(self):
        args = ADMIN.parser().parse_args(["--url", "http://127.0.0.1", "archive", "--game", "demo",
                                          "--keep-days", "30", "--max-batches", "1"])
        client = FakeClient([{"archivedTasks": 100, "redactedEvents": 100}])
        self.assertFalse(ADMIN.execute(client, args)["exhausted"])
        self.assertEqual(len(client.calls), 1)

    def test_control_update_uses_optimistic_revision(self):
        args = ADMIN.parser().parse_args(["--url", "http://127.0.0.1", "control-set", "--game", "demo",
                                          "--expected-revision", "4", "--delivery-enabled", "false"])
        client = FakeClient([{"revision": 5}])
        ADMIN.execute(client, args)
        self.assertEqual(client.calls, [("POST", "/admin/control/demo", {"expectedRevision": 4, "deliveryEnabled": False})])

    def test_lookup_preserves_full_business_identity(self):
        args = ADMIN.parser().parse_args(["--url", "http://127.0.0.1", "lookup", "--game", "demo",
            "--task-type", "payment", "--namespace", "merchant-a", "--biz-key", "order:1"])
        client = FakeClient([{}])
        ADMIN.execute(client, args)
        self.assertEqual(client.calls[0][1], "/admin/events/demo/payment/merchant-a/order:1")

    def test_task_page_uses_only_whitelisted_filters_and_preserves_cursor(self):
        args = ADMIN.parser().parse_args(["--url", "http://127.0.0.1", "tasks", "--game", "demo", "--status", "3",
            "--server-id", "5", "--created-from", "0", "--created-before", "100", "--limit", "2", "--after", "7"])
        reply = {"items": [{"id": "8", "status": 3}], "nextAfter": "8"}
        client = FakeClient([reply])
        self.assertEqual(ADMIN.execute(client, args), reply)
        path = urlsplit(client.calls[0][1])
        self.assertEqual(path.path, "/admin/tasks/demo/7")
        self.assertEqual(parse_qs(path.query), {"status": ["3"], "serverId": ["5"], "createdFrom": ["0"],
                                              "createdBefore": ["100"], "limit": ["2"]})

    def test_task_filter_boundaries_fail_before_any_request(self):
        for field, value in (("status", "4"), ("server-id", "0"), ("server-id", str(2**32)),
                             ("created-from", "-1"), ("created-before", str(ADMIN.MAX_UNIX_SECONDS + 1)),
                             ("limit", "101"), ("after", "-1")):
            with self.subTest(field=field), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ADMIN.parser().parse_args(["--url", "http://127.0.0.1", "tasks", "--game", "demo", "--" + field, value])
        args = ADMIN.parser().parse_args(["--url", "http://127.0.0.1", "tasks", "--game", "demo",
                                          "--created-from", "100", "--created-before", "100"])
        client = FakeClient([])
        with self.assertRaises(ValueError):
            ADMIN.execute(client, args)
        self.assertEqual(client.calls, [])

    def test_explicit_replay_sends_empty_body_and_never_retries_forbidden_or_conflict(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            status = 403
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                observed.append((self.path, body, self.headers.get("Authorization")))
                response = json.dumps({"untrustedDetail": self.headers.get("Authorization")}).encode()
                self.send_response(self.status)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token = "synthetic.private.management.jwt"
        try:
            for status in (403, 409, 200):
                Handler.status = status
                output, errors = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, {"GAMEHUB_ADMIN_TOKEN": token}), \
                        patch("sys.argv", ["hub_admin.py", "--url", "http://127.0.0.1:" + str(server.server_port),
                                           "replay", "--game", "demo", "--task-id", "42"]), \
                        redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(ADMIN.main(), 1)
                if status != 200:
                    self.assertIn(str(status), errors.getvalue())
                self.assertNotIn(token, output.getvalue() + errors.getvalue())
            self.assertEqual(len(observed), 3)
            self.assertTrue(all(path == "/admin/tasks/demo/42/replay" and body == b"" and authorization == "Bearer " + token
                                for path, body, authorization in observed))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_budget_exhaustion_prevents_network_request(self):
        client = ADMIN.Client("http://127.0.0.1", "synthetic.jwt.value", budget=1)
        client.deadline = time.monotonic() - 1
        with self.assertRaises(TimeoutError):
            client.request("GET", "/admin/health")

    def test_response_status_and_size_are_checked_without_disclosing_body(self):
        class Response:
            status = 302
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, size):
                return b"{}"
        class Connection:
            sock = None
            def __init__(self, *args, **kwargs):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        client = ADMIN.Client("http://127.0.0.1", "synthetic.jwt.value", connection_factory=Connection)
        with self.assertRaisesRegex(ValueError, "302"):
            client.request("GET", "/admin/health")
        Response.status = 200
        with patch.object(ADMIN, "MAX_RESPONSE_BYTES", 1):
            with self.assertRaises(ValueError):
                client.request("GET", "/admin/health")

    def test_slow_response_cannot_extend_absolute_request_budget(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "7")
                self.end_headers()
                try:
                    for byte in b'{"a":1}':
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.08)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ADMIN.Client("http://127.0.0.1:" + str(server.server_port), "synthetic.jwt.value", timeout=0.15)
            began = time.monotonic()
            with self.assertRaises((ValueError, TimeoutError)):
                client.request("GET", "/admin/health")
            self.assertLess(time.monotonic() - began, 0.7)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

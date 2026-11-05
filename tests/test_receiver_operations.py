"""演示接收端的轮换、配置持久化、补拉与回报重试回归，只用合成数据。"""

from contextlib import closing
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("receiver_operations_example", ROOT / "examples/demo_receiver.py")
RECEIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIVER)
SECRET = "synthetic-current-secret-" + "a" * 32
PREVIOUS = "synthetic-previous-secret-" + "b" * 32


def config(version, content=None):
    """生成与 Hub 约定一致的合成配置响应。"""
    content = content or {"syntheticSetting": version}
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"version": version, "content": content, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}


class ReceiverOperationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ".local" / ("receiver-test-" + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory))

    def receiver(self, **kwargs):
        """创建有真实 SQLite 但没有配置同步线程的接收端。"""
        value = RECEIVER.DemoReceiver(("127.0.0.1", 0), self.directory / "receiver.db", SECRET, **kwargs)
        self.addCleanup(value.server_close)
        return value

    def test_key_id_selection_and_legacy_window_are_explicit(self):
        server = self.receiver(key_id="current", previous_keys={"previous": PREVIOUS}, legacy_secret=PREVIOUS)
        timestamp = str(int(time.time()))
        for key_id, secret in (("current", SECRET), ("previous", PREVIOUS), ("", PREVIOUS)):
            headers = {"X-Key-Id": key_id, "X-Timestamp": timestamp,
                       "X-Signature": RECEIVER.signature(secret, timestamp, "POST", "/internal/gamehub", b"{}", key_id)}
            self.assertTrue(server.authenticate(headers, "POST", "/internal/gamehub", b"{}"))
            self.assertFalse(server.authenticate(dict(headers, **{"X-Key-Id": "unknown"}),
                                                 "POST", "/internal/gamehub", b"{}"))
        server.legacy_secret = ""
        self.assertEqual(server.verification_key(""), "")

    def test_config_is_monotonic_and_survives_receiver_restart(self):
        server = self.receiver()
        with patch.object(server, "hub_request", side_effect=[config(2), config(1)]):
            self.assertEqual(server.apply_config(2)["version"], 2)
            self.assertEqual(server.apply_config(1)["version"], 2)
        server.server_close()
        restarted = self.receiver()
        self.assertEqual(restarted.config_status()["version"], 2)

    def test_bad_hash_and_same_version_conflict_do_not_mutate_config(self):
        server = self.receiver()
        with patch.object(server, "hub_request", return_value=config(1)):
            original = server.apply_config(1)
        for value in (dict(config(2), sha256="0" * 64), config(1, {"syntheticSetting": "different"})):
            with patch.object(server, "hub_request", return_value=value), self.assertRaises(ValueError):
                server.apply_config(value["version"])
            self.assertEqual(server.config_status(), original)

    def test_exact_float_content_text_is_used_for_hash(self):
        server = self.receiver()
        canonical = '{"x":1E0}'
        value = {"version": 1, "content": {"x": 1.0}, "canonicalContent": canonical,
                 "sha256": hashlib.sha256(canonical.encode()).hexdigest()}
        with patch.object(server, "hub_request", return_value=value):
            self.assertEqual(server.apply_config(1)["contentHash"], value["sha256"])
        with closing(sqlite3.connect(server.database)) as connection:
            self.assertEqual(connection.execute("SELECT content FROM synthetic_config").fetchone()[0], canonical)
        del value["canonicalContent"]
        with patch.object(server, "hub_request", return_value=value), self.assertRaises(ValueError):
            server.apply_config(1)

    def test_large_valid_config_response_and_hard_limit(self):
        value = config(1, {"syntheticText": "x" * 40000})
        value["canonicalContent"] = json.dumps(value["content"], separators=(",", ":"))
        class Handler(BaseHTTPRequestHandler):
            content = json.dumps(value).encode()
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(self.content)))
                self.end_headers()
                try:
                    self.wfile.write(self.content)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        hub = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=hub.serve_forever, daemon=True)
        thread.start()
        server = self.receiver()
        server.hub = urlsplit("http://127.0.0.1:" + str(hub.server_port))
        try:
            self.assertGreater(len(Handler.content), 65536)
            self.assertEqual(server.apply_config(1)["version"], 1)
            Handler.content = b" " * (RECEIVER.MAX_CONFIG_RESPONSE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "大小限制"):
                server.hub_request("GET", "/synthetic")
        finally:
            hub.shutdown()
            hub.server_close()
            thread.join(timeout=2)

    def test_dripping_headers_are_interrupted_by_absolute_expiry(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Synthetic: ")
                    self.wfile.flush()
                    for _ in range(30):
                        self.wfile.write(b"a")
                        self.wfile.flush()
                        time.sleep(0.04)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        hub = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=hub.serve_forever, daemon=True)
        thread.start()
        server = self.receiver()
        server.hub = urlsplit("http://127.0.0.1:" + str(hub.server_port))
        original_timer = threading.Timer
        began = time.monotonic()
        try:
            with patch.object(RECEIVER.threading, "Timer", side_effect=lambda interval, callback:
                              original_timer(min(interval, 0.15), callback)):
                with self.assertRaises((OSError, ValueError, http.client.HTTPException)):
                    server.hub_request("GET", "/synthetic")
            self.assertLess(time.monotonic() - began, 0.9)
        finally:
            hub.shutdown()
            hub.server_close()
            thread.join(timeout=2)

    def test_startup_schedules_latest_config_sync(self):
        with patch.object(RECEIVER.threading, "Thread") as thread:
            server = self.receiver(hub_url="http://127.0.0.1:8443")
            thread.assert_called_once_with(target=server.sync_config, daemon=True)
            thread.return_value.start.assert_called_once()

    def test_sync_retries_report_and_always_fetches_latest(self):
        server = self.receiver()
        class Stop:
            cycles = 0
            def is_set(self):
                return self.cycles >= 2
            def wait(self, seconds):
                self.cycles += 1
        stop = Stop()
        with patch.object(server, "config_stop", stop), patch.object(server, "apply_config") as apply, \
                patch.object(server, "report_config", side_effect=[OSError("合成回报故障"), None]) as report:
            server.sync_config()
        self.assertEqual([call.args for call in apply.call_args_list], [(0,), (0,)])
        self.assertEqual(report.call_count, 2)

    def test_report_failure_does_not_ack_and_retry_keeps_one_inbox_record(self):
        server = self.receiver()
        server.hub = urlsplit("http://127.0.0.1:8443")
        calls = []
        def hub_request(method, path, body=b""):
            calls.append(method)
            if method == "GET":
                return config(2)
            if calls.count("POST") == 1:
                raise OSError("合成回报故障")
            return {}
        server.hub_request = hub_request
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=2)))
        body = json.dumps({"gameId": "demo", "serverId": 1, "taskType": "config_push", "namespace": "admin",
                           "bizKey": "config:2", "payload": {"version": 2}, "taskId": "123"}).encode()
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-Timestamp": timestamp,
                   "X-Signature": RECEIVER.signature(SECRET, timestamp, "POST", "/internal/gamehub", body)}
        for expected in (503, 200):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as connection:
                connection.request("POST", "/internal/gamehub", body, headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, expected)
        with closing(sqlite3.connect(server.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
        self.assertEqual(server.config_status()["version"], 2)


if __name__ == "__main__":
    unittest.main()

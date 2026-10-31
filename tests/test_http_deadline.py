"""用真实回环 HTTP 慢滴流检查总截止、线程池和失败后的清理。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import integration


class HttpPeer:
    """按顺序处理真实连接；退出时唤醒等待并关闭本次 socket 和线程。"""

    def __init__(self, handler, receive_buffer=None):
        self.handler = handler
        self.stop = threading.Event()
        self.listener = socket.socket()
        if receive_buffer:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.connection = None
        self.errors = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        """顺序接收请求头并交给 handler，无返回值；预期的断连不作为服务端失败。"""
        index = 0
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connection = connection
            try:
                with connection:
                    connection.settimeout(1)
                    headers = b""
                    while b"\r\n\r\n" not in headers:
                        data = connection.recv(4096)
                        if not data:
                            break
                        headers += data
                    index += 1
                    self.handler(connection, self.stop, index)
            except OSError:
                pass
            except Exception as error:
                self.errors.append(error)
            finally:
                self.connection = None

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.listener.close()
        connection = self.connection
        if connection:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("HTTP 测试线程没有退出")
        if self.errors:
            raise self.errors[0]


def drip(connection, stop, fragments):
    """每 20 毫秒发送一个 fragment，无返回值；测试结束时立即退出。"""
    for fragment in fragments:
        if stop.wait(0.02):
            return
        connection.sendall(fragment)


class HttpDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.Fixture(Path("unused-gamehub"), Path("unused-mysql"))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.fixture.run_deadline = None
        for child in self.fixture.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        for log in self.fixture.logs:
            log.close()
        for path in (self.fixture.path, self.fixture.diagnostic_path):
            if path and path.exists():
                self.assertEqual(path.resolve().parent, self.fixture.base)
                shutil.rmtree(path)

    def expect_run_timeout(self, action):
        """action 必须在操作内部报整轮到期，不能等正常返回后由上下文出口补报。"""
        returned = False
        started = time.monotonic()
        try:
            with self.assertRaises(integration.IntegrationDeadlineExceeded):
                with integration.RunDeadline(0.15) as deadline:
                    self.fixture.run_deadline = deadline
                    action()
                    returned = True
        finally:
            self.fixture.run_deadline = None
        self.assertFalse(returned, "网络操作在截止点后仍正常返回")
        self.assertLess(time.monotonic() - started, 2)

    def test_request_status_and_headers_cannot_extend_deadline_by_dripping(self):
        responses = (
            (b"", b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
            (b"HTTP/1.1 200 OK\r\n", b"X-Synthetic: " + b"x" * 40 + b"\r\nContent-Length: 0\r\n\r\n"),
        )
        for prefix, tail in responses:
            with self.subTest(prefix=bool(prefix)):
                def handler(connection, stop, _index):
                    connection.sendall(prefix)
                    drip(connection, stop, (bytes((byte,)) for byte in tail))
                with HttpPeer(handler) as peer:
                    self.fixture.http_port = peer.port
                    self.expect_run_timeout(lambda: self.fixture.request("GET", "/synthetic"))

    def test_request_length_chunked_and_eof_bodies_share_absolute_deadline(self):
        responses = (
            (b"Content-Length: 30\r\n", [b"x"] * 30),
            (b"Transfer-Encoding: chunked\r\n", [b"1\r\nx\r\n"] * 30 + [b"0\r\n\r\n"]),
            (b"Connection: close\r\n", [b"x"] * 30),
        )
        for headers, fragments in responses:
            with self.subTest(headers=headers):
                def handler(connection, stop, _index):
                    connection.sendall(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n")
                    drip(connection, stop, fragments)
                with HttpPeer(handler) as peer:
                    self.fixture.http_port = peer.port
                    self.expect_run_timeout(lambda: self.fixture.request("GET", "/synthetic"))

    def test_request_sending_counts_towards_the_same_deadline(self):
        def handler(connection, stop, _index):
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            stop.wait(2)
        with HttpPeer(handler, receive_buffer=4096) as peer:
            self.fixture.http_port = peer.port
            payload = b"x" * (16 * 1024 * 1024)
            original_connect = integration.HttpDeadline.connect
            def connect(deadline, port):
                connection = original_connect(deadline, port)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                return connection
            with patch.object(integration.HttpDeadline, "connect", autospec=True, side_effect=connect), \
                    patch.object(integration.http.client.HTTPConnection, "getresponse") as getresponse:
                self.expect_run_timeout(lambda: self.fixture.request("POST", "/synthetic", raw=payload))
            getresponse.assert_not_called()

    def test_burst_recomputes_remaining_budget_across_responses(self):
        def handler(connection, stop, _index):
            response = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
            drip(connection, stop, [response] * 20)
            connection.sendall(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        with HttpPeer(handler) as peer, redirect_stdout(io.StringIO()):
            self.fixture.http_port = peer.port
            self.expect_run_timeout(lambda: self.fixture.callback_burst("/synthetic", 21))

    def test_path_boundary_eof_wait_obeys_run_deadline(self):
        def handler(connection, stop, _index):
            connection.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            stop.wait(2)
        with HttpPeer(handler) as peer:
            self.fixture.http_port = peer.port
            self.expect_run_timeout(lambda: integration.verify_path_boundaries(self.fixture))

    def test_thread_pool_uses_fixture_deadline_and_timeout_is_not_a_network_retry(self):
        def handler(connection, stop, _index):
            drip(connection, stop, [b"H"] * 30)
        with HttpPeer(handler) as peer, ThreadPoolExecutor(max_workers=1) as pool:
            self.fixture.http_port = peer.port
            def worker():
                self.assertIsNone(integration.ACTIVE_DEADLINE.get())
                try:
                    return self.fixture.request("GET", "/synthetic")
                except (OSError, TimeoutError, integration.http.client.HTTPException):
                    self.fail("整轮到期被普通网络错误分支吞掉")
            self.expect_run_timeout(lambda: pool.submit(worker).result(timeout=2))

    def test_single_request_timeout_remains_bounded_without_a_run_deadline(self):
        def handler(connection, stop, _index):
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n")
            drip(connection, stop, [b"x"] * 30)
        with HttpPeer(handler) as peer:
            self.fixture.http_port = peer.port
            with patch.object(self.fixture, "http_deadline", side_effect=lambda: integration.HttpDeadline(0.1)):
                with self.assertRaises(TimeoutError):
                    self.fixture.request("GET", "/synthetic")

    def test_normal_response_and_expired_connect_budget(self):
        def handler(connection, _stop, _index):
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")
        with HttpPeer(handler) as peer:
            self.fixture.http_port = peer.port
            self.assertEqual(self.fixture.request("GET", "/synthetic"), (200, {"ok": True}))
            deadline = integration.HttpDeadline(1)
            deadline.deadline = time.monotonic() - 1
            with self.assertRaises(TimeoutError):
                deadline.connect(peer.port)

    def test_http_timeout_keeps_diagnostics_and_cleanup_uses_a_fresh_budget(self):
        fixture = self.fixture
        def handler(connection, stop, index):
            if index == 1:
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n")
                drip(connection, stop, [b"x"] * 30)
            else:
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        fixture.sql = Mock(return_value="")
        def start():
            fixture.hub = fixture.launch([sys.executable, "-c", "import time; time.sleep(30)"], "hub")
        fixture.start = start
        with HttpPeer(handler) as peer:
            fixture.http_port = peer.port
            with patch.object(integration, "verify", lambda current: current.request("GET", "/synthetic")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                    self.assertRaises(integration.IntegrationDeadlineExceeded):
                integration.run_fixture(fixture, timeout_seconds=0.2)
        report = json.loads((fixture.diagnostic_path / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failureType"], "IntegrationDeadlineExceeded")
        self.assertEqual(report["ready"], {"status": 200})
        self.assertIsNotNone(fixture.hub.poll())
        self.assertFalse(fixture.path.exists())
        self.assertIsNone(fixture.run_deadline)


if __name__ == "__main__":
    unittest.main()

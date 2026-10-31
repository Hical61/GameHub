"""用原始 HTTP 响应测试三种实体读取路径和连接复用，不启动数据库。"""

import argparse
import hashlib
import json
from pathlib import Path
import socketserver
import subprocess
import sys
import threading
import time
import unittest


CLIENT = None
LIMIT = 1024
MAX_INFORMATIONAL_RESPONSES = 16
MAX_RESPONSE_HEADER_BYTES = 64 * 1024


def response(mode, body, declared=None):
    """按指定 framing 返回原始报文；declared 可模拟只声明超长而不发送实体。"""
    if mode == "chunked":
        chunks = (body[:len(body) // 2], body[len(body) // 2:])
        framed = b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in chunks if chunk)
        return b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + framed + b"0\r\n\r\n"
    if mode == "eof":
        return b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body
    length = len(body) if declared is None else declared
    return f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n".encode() + body


def informational(status=103, padding=0):
    """中间响应只带测试头，最终响应不应保留这些字段。"""
    return (f"HTTP/1.1 {status} Interim\r\nX-Interim: ".encode()
            + b"x" * padding + b"ignored\r\n\r\n")


class RawHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        with self.server.lock:
            self.server.connections += 1
        try:
            while self.rfile.readline():
                while self.rfile.readline() not in (b"\r\n", b"", b"\n"):
                    pass
                with self.server.lock:
                    index = self.server.requests
                    self.server.requests += 1
                if index >= len(self.server.responses):
                    return
                raw, close = self.server.responses[index]
                # 分段响应允许在中间响应、最终头和实体之间制造真实的拆包与延迟。
                parts = [(raw, 0)] if isinstance(raw, bytes) else raw
                for data, delay in parts:
                    self.wfile.write(data)
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay)
                if close:
                    return
        except (ConnectionError, TimeoutError, OSError):
            pass


class RawServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, responses):
        super().__init__(("127.0.0.1", 0), RawHandler)
        self.responses = responses
        self.connections = 0
        self.requests = 0
        self.lock = threading.Lock()

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=3)


class ResponseLimitTests(unittest.TestCase):
    def run_client(self, server, limits, request_timeout_ms=2000):
        command = [str(CLIENT), str(server.server_address[1]), *map(str, limits),
                   f"--request-timeout-ms={request_timeout_ms}"]
        result = subprocess.run(command,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(len(records), len(limits))
        return records

    def test_each_framing_checks_below_equal_and_above_limit(self):
        for mode in ("length", "chunked", "eof"):
            for size in (LIMIT - 1, LIMIT, LIMIT + 1):
                with self.subTest(mode=mode, size=size), RawServer([(response(mode, b"x" * size), mode == "eof")]) as server:
                    result = self.run_client(server, [LIMIT])[0]
                    self.assertEqual(result["tooLarge"], size > LIMIT)
                    self.assertEqual(result["error"], size > LIMIT)
                    self.assertEqual(result["bodyBytes"], 0 if size > LIMIT else size)
                    if size > LIMIT:
                        self.assertEqual(result["idle"], 0)

    def test_content_length_rejects_before_waiting_for_body(self):
        with RawServer([(response("length", b"", declared=LIMIT + 1), False)]) as server:
            result = self.run_client(server, [LIMIT])[0]
            self.assertTrue(result["tooLarge"])
            self.assertEqual(result["bodyBytes"], 0)

    def test_large_fast_response_is_not_retained(self):
        for mode in ("length", "chunked", "eof"):
            with self.subTest(mode=mode), RawServer([(response(mode, b"x" * (4 * 1024 * 1024)), mode == "eof")]) as server:
                result = self.run_client(server, [LIMIT])[0]
                self.assertTrue(result["tooLarge"])
                self.assertEqual(result["bodyBytes"], 0)

    def test_over_limit_connection_is_discarded_then_next_request_succeeds(self):
        frames = [(response("length", b"ok"), False),
                  (response("chunked", b"x" * (LIMIT + 1)), False),
                  (response("length", b"ok"), False)]
        with RawServer(frames) as server:
            records = self.run_client(server, [LIMIT] * 3)
            self.assertEqual([item["tooLarge"] for item in records], [False, True, False])
            self.assertEqual([item["idle"] for item in records], [1, 0, 1])
            self.assertEqual(server.connections, 2)

    def test_different_limits_cannot_reuse_a_wider_connection(self):
        with RawServer([(response("length", b"x" * 1500), False)] * 3) as server:
            records = self.run_client(server, [2048, LIMIT, 2048])
            self.assertEqual([item["tooLarge"] for item in records], [False, True, False])
            self.assertEqual(server.connections, 2)

    def test_complete_chunked_ending_allows_next_response_on_same_connection(self):
        with RawServer([(response("chunked", b"first"), False),
                        (response("length", b"second"), False)]) as server:
            records = self.run_client(server, [LIMIT, LIMIT])
            self.assertFalse(any(item["error"] for item in records))
            self.assertEqual([item["bodyBytes"] for item in records], [5, 6])
            self.assertEqual(server.connections, 1)

    def test_zero_limit_accepts_only_empty_body(self):
        for size in (0, 1):
            with self.subTest(size=size), RawServer([(response("length", b"x" * size), False)]) as server:
                result = self.run_client(server, [0])[0]
                self.assertEqual(result["tooLarge"], size != 0)

    def test_informational_responses_keep_final_body_and_next_request_aligned(self):
        for mode in ("length", "chunked", "eof"):
            for split in (False, True):
                with self.subTest(mode=mode, split=split):
                    raw = b"".join(informational(status) for status in (100, 102, 103, 199))
                    raw += response(mode, b"first-confirmation")
                    if split:
                        cuts = (9, len(informational()) + 3, len(raw) - 4)
                        boundaries = (0, *cuts, len(raw))
                        raw = [(raw[start:end], 0.01) for start, end in zip(boundaries, boundaries[1:])]
                    frames = [(raw, mode == "eof"), (response("length", b"second-confirmation"), False)]
                    with RawServer(frames) as server:
                        records = self.run_client(server, [LIMIT, LIMIT])
                        self.assertEqual([item["status"] for item in records], [200, 200])
                        self.assertFalse(any(item["error"] or item["interimHeader"] for item in records))
                        self.assertEqual([item["bodySha256"] for item in records],
                                         [hashlib.sha256(body).hexdigest() for body in
                                          (b"first-confirmation", b"second-confirmation")])
                        self.assertEqual(server.connections, 2 if mode == "eof" else 1)

    def test_informational_responses_preserve_body_limit_for_each_framing(self):
        for mode in ("length", "chunked", "eof"):
            with self.subTest(mode=mode), RawServer([
                    (informational() + response(mode, b"x" * (LIMIT + 1)), mode == "eof")]) as server:
                result = self.run_client(server, [LIMIT])[0]
                self.assertEqual(result["status"], 200)
                self.assertTrue(result["tooLarge"])
                self.assertEqual(result["bodyBytes"], 0)
                self.assertEqual(result["idle"], 0)

    def test_interim_header_filling_initial_buffer_keeps_writable_storage(self):
        initial_buffer_bytes = 4096
        interim = informational(padding=initial_buffer_bytes - len(informational()))
        parts = [(interim, 0.01), (response("length", b"final"), 0)]
        with RawServer([(parts, False)]) as server:
            result = self.run_client(server, [LIMIT])[0]
            self.assertEqual(result["status"], 200)
            self.assertFalse(result["error"])
            self.assertEqual(result["bodySha256"], hashlib.sha256(b"final").hexdigest())

    def test_informational_response_count_has_a_fixed_limit(self):
        for count in (MAX_INFORMATIONAL_RESPONSES, MAX_INFORMATIONAL_RESPONSES + 1):
            with self.subTest(count=count), RawServer([
                    (informational() * count + response("length", b"ok"), False)]) as server:
                result = self.run_client(server, [LIMIT])[0]
                rejected = count > MAX_INFORMATIONAL_RESPONSES
                self.assertEqual(result["error"], rejected)
                self.assertEqual(result["idle"], 0 if rejected else 1)
                self.assertFalse(result["tooLarge"])
                self.assertEqual(result["bodyBytes"], 0 if rejected else 2)

    def test_all_response_headers_share_one_byte_budget(self):
        final = response("length", b"ok")
        final_header_bytes = final.index(b"\r\n\r\n") + 4
        padding = MAX_RESPONSE_HEADER_BYTES - final_header_bytes - len(informational())
        for extra in (0, 1):
            with self.subTest(extra=extra), RawServer([
                    (informational(padding=padding + extra) + final, False)]) as server:
                result = self.run_client(server, [LIMIT])[0]
                self.assertEqual(result["error"], extra != 0)
                self.assertEqual(result["bodyBytes"], 0 if extra else 2)
                self.assertFalse(result["tooLarge"])

    def test_incomplete_final_header_cannot_exceed_remaining_header_budget(self):
        interim = informational(padding=MAX_RESPONSE_HEADER_BYTES - len(informational()) - 8)
        with RawServer([(interim + b"HTTP/1.1 200 " + b"x" * 64, False)]) as server:
            result = self.run_client(server, [LIMIT])[0]
            self.assertTrue(result["error"])
            self.assertEqual(result["idle"], 0)
            self.assertFalse(result["timedOut"])

    def test_informational_responses_do_not_restart_request_deadline(self):
        parts = [(informational(102), 0.25), (informational(103), 0.25),
                 (response("length", b"late"), 0)]
        with RawServer([(parts, False)]) as server:
            result = self.run_client(server, [LIMIT], request_timeout_ms=400)[0]
            self.assertTrue(result["error"])
            self.assertTrue(result["timedOut"])
            self.assertFalse(result["tooLarge"])
            self.assertEqual(result["bodyBytes"], 0)
            self.assertEqual(result["idle"], 0)

    def test_protocol_upgrade_is_rejected_and_connection_discarded(self):
        upgrade = (b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                   b"Upgrade: websocket\r\n\r\nupgraded bytes")
        frames = [(response("length", b"before"), False), (upgrade, False),
                  (response("length", b"after"), False)]
        with RawServer(frames) as server:
            records = self.run_client(server, [LIMIT] * 3)
            self.assertEqual([item["status"] for item in records], [200, 101, 200])
            self.assertEqual([item["error"] for item in records], [False, True, False])
            self.assertEqual([item["idle"] for item in records], [1, 0, 1])
            self.assertEqual(server.connections, 2)

    def test_disconnect_after_interim_response_does_not_replay_request_in_pool(self):
        frames = [(response("length", b"before"), False), (informational(), True),
                  (response("length", b"after"), False)]
        with RawServer(frames) as server:
            records = self.run_client(server, [LIMIT] * 3)
            self.assertEqual([item["status"] for item in records], [200, 103, 200])
            self.assertEqual([item["error"] for item in records], [False, True, False])
            self.assertEqual([item["idle"] for item in records], [1, 0, 1])
            self.assertEqual(records[-1]["bodySha256"], hashlib.sha256(b"after").hexdigest())
            self.assertEqual(server.requests, 3, "已收到中间响应的请求不应由池再次发送")
            self.assertEqual(server.connections, 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, required=True)
    args = parser.parse_args()
    CLIENT = args.client.resolve()
    unittest.main(argv=[sys.argv[0]])

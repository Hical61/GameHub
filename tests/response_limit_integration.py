"""检查响应限额和中间响应处理，所有投递仍只产生一条模拟流水。"""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time
from unittest.mock import patch

import integration
from demo_receiver import signature


class GameHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def handle(self):
        self.connection.settimeout(5)
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536 or self.path != "/internal/gamehub":
                self.close_connection = True
                return
            body = self.rfile.read(size)
            timestamp = self.headers.get("X-Timestamp", "")
            expected = signature(self.server.secret, timestamp, "POST", self.path, body)
            if abs(int(timestamp) - int(time.time())) > 300 or not hmac.compare_digest(
                    expected, self.headers.get("X-Signature", "")):
                self.close_connection = True
                return
            envelope = json.loads(body)
            self.server.receiver.commit(envelope)
            with self.server.mode_lock:
                mode, over_limit = self.server.mode, self.server.over_limit
                informational = self.server.informational
                self.server.deliveries += 1
            ack = json.dumps({"taskId": envelope["taskId"], "committed": True}, separators=(",", ":")).encode()
            target_size = self.server.limit + int(over_limit)
            body = ack + b" " * (target_size - len(ack))
            for status in informational:
                self.wfile.write(f"HTTP/1.1 {status} Interim\r\nX-Interim: ignored\r\n\r\n".encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if mode == "length":
                self.send_header("Content-Length", str(len(body)))
            elif mode == "chunked":
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if mode == "chunked":
                midpoint = len(body) // 2
                for chunk in (body[:midpoint], body[midpoint:]):
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(body)
            self.wfile.flush()
        except (ConnectionError, TimeoutError, ValueError, KeyError):
            self.close_connection = True


class GameServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fixture):
        super().__init__(("127.0.0.1", 0), GameHandler)
        self.receiver = fixture.receivers[0]
        self.secret = fixture.env["GAMEHUB_DEMO_OUTBOUND_SECRET"]
        self.limit = 1024
        self.mode_lock = threading.Lock()
        self.mode = "length"
        self.over_limit = True
        self.informational = ()
        self.deliveries = 0

    def set_mode(self, mode, over_limit, informational=()):
        with self.mode_lock:
            self.mode, self.over_limit = mode, over_limit
            self.informational = informational


def verify_config_limits(fixture, config):
    """检查可省略的默认值和配置边界，不输出配置、凭据或命令行原文。"""
    for index, (size, accepted) in enumerate(((None, True), (63, False), (64, True),
                                             (1024 * 1024, True), (1024 * 1024 + 1, False))):
        variant = json.loads(json.dumps(config))
        if size is None:
            variant["worker"].pop("maxResponseBodyBytes", None)
        else:
            variant["worker"]["maxResponseBodyBytes"] = size
        path = fixture.path / f"response-config-{index}.json"
        path.write_text(json.dumps(variant), encoding="utf-8")
        result = subprocess.run([str(fixture.binary), "--config", str(path), "--check-config"],
                                capture_output=True, env=fixture.env, timeout=fixture.operation_timeout(10))
        assert (result.returncode == 0) == accepted, "响应上限配置边界检查失败"


def verify_informational_responses(fixture, server, config):
    """重现同连接六笔订单，检查最终确认没有串到下一笔请求。"""
    fixture.hub.terminate()
    fixture.hub.wait(timeout=fixture.operation_timeout(10))
    config["worker"].update(concurrency=1, maxAttempts=3)
    fixture.config.write_text(json.dumps(config), encoding="utf-8")
    fixture.start_hub()
    cases = (("length", (103,), 6), ("chunked", (100, 102, 103, 199), 1), ("eof", (103,), 1))
    for mode, statuses, count in cases:
        server.set_mode(mode, False, informational=statuses)
        with server.mode_lock:
            before = server.deliveries
        events = []
        for index in range(count):
            key = f"informational-{mode}-{index}"
            status, body = fixture.request("POST", "/api/callback/demo/demo", integration.order(key), signed=True)
            assert status == 200
            events.append((key, int(body["eventId"])))
        integration.wait_for(lambda: all(fixture.tasks(event_id) == [2] for _, event_id in events),
                             "中间响应之后读取正确的最终业务确认")
        for key, event_id in events:
            attempts = fixture.sql(f"SELECT attempts FROM gamehub.delivery_task WHERE event_id={event_id}")
            assert attempts == "1", "中间响应触发了不必要的任务重试"
            assert fixture.ledger(1, key) == 1, "中间响应产生重复经济流水"
        with server.mode_lock:
            assert server.deliveries - before == count, "最终确认串到其他请求，导致重复 HTTP 投递"
        print(f"通过：{mode} 中间响应后 {count} 笔任务全部首次确认，流水各为一条")


def verify_response_limits(fixture):
    fixture.hub.terminate()
    fixture.hub.wait(timeout=fixture.operation_timeout(10))
    server = GameServer(fixture)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    try:
        thread.start()
        config = json.loads(fixture.config.read_text(encoding="utf-8"))
        config["worker"].update(maxAttempts=1, maxResponseBodyBytes=server.limit)
        config["games"][0]["servers"][0]["port"] = server.server_port
        fixture.config.write_text(json.dumps(config), encoding="utf-8")
        verify_config_limits(fixture, config)
        fixture.start_hub()
        for mode in ("length", "chunked", "eof"):
            key = "response-limit-" + mode
            server.set_mode(mode, True)
            status, body = fixture.request("POST", "/api/callback/demo/demo", integration.order(key), signed=True)
            assert status == 200
            event_id = body["eventId"]
            integration.wait_for(lambda: fixture.tasks(event_id) == [3], "超限响应转为失败")
            state = fixture.sql(f"SELECT id,last_error FROM gamehub.delivery_task WHERE event_id={int(event_id)}")
            task_id, error = state.split("\t")
            assert error == "response_too_large", "超限未记录固定错误类别"
            assert fixture.ledger(1, key) == 1
            server.set_mode(mode, False)
            status, _ = fixture.request("POST", f"/admin/tasks/demo/{task_id}/replay", admin=fixture.admin())
            assert status == 200
            integration.wait_for(lambda: fixture.tasks(event_id) == [2], "等于限额的响应确认成功")
            assert fixture.ledger(1, key) == 1, "重排产生重复经济流水"
            print(f"通过：{mode} 超限不确认，等于限额可确认，重排没有重复流水")
        verify_informational_responses(fixture, server, config)
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread.ident:
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--mysql-bin", type=Path, required=True)
    args = parser.parse_args()
    fixture = integration.Fixture(args.binary, args.mysql_bin)
    with patch.object(integration, "verify", verify_response_limits):
        integration.run_fixture(fixture)


if __name__ == "__main__":
    main()

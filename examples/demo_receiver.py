"""用假订单演示游戏服接收通知，SQLite 事务保证去重和记账一起提交。"""

import argparse
from contextlib import closing
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def signature(secret, timestamp, method, path, body):
    """按 GameHub 演示协议计算 HMAC，签名不含 HTTP 分块帧。"""
    digest = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}\n{method}\n{path}\n{digest}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class DemoReceiver(ThreadingHTTPServer):
    """一个实例对应一个游戏区服，故障开关给集成测试用。"""

    daemon_threads = True

    def __init__(self, address, database, secret, game_id="demo", server_id=1):
        super().__init__(address, Handler)
        self.database = str(database)
        self.secret = secret
        self.game_id = game_id
        self.server_id = server_id
        self.fail = False
        self.bad_ack = False
        self.drop_once = False
        self.pause = None
        self.requests = 0
        self.lock = threading.Lock()
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox(
                    game_id TEXT, server_id INTEGER, task_type TEXT, namespace_key TEXT, biz_key TEXT,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY(game_id,server_id,task_type,namespace_key,biz_key));
                CREATE TABLE IF NOT EXISTS synthetic_ledger(
                    game_id TEXT, server_id INTEGER, namespace_key TEXT, biz_key TEXT, amount_minor INTEGER,
                    PRIMARY KEY(game_id,server_id,namespace_key,biz_key));
            """)

    def commit(self, envelope):
        """把去重记录和模拟流水写进同一事务，出错时一起回滚。"""
        identity = (envelope["gameId"], envelope["serverId"], envelope["taskType"],
                    envelope["namespace"], envelope["bizKey"])
        digest = hashlib.sha256(json.dumps(envelope["payload"], sort_keys=True,
                                           separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload_hash FROM inbox WHERE game_id=? AND server_id=? "
                                  "AND task_type=? AND namespace_key=? AND biz_key=?", identity).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError("相同业务键的内容不一致")
                return
            db.execute("INSERT INTO inbox VALUES(?,?,?,?,?,?)", (*identity, digest))
            if envelope["taskType"] == "recharge_notify":
                amount = envelope["payload"]["amountMinor"]
                if type(amount) is not int or not 1 <= amount <= 1_000_000_000_000:
                    raise ValueError("金额无效")
                db.execute("INSERT INTO synthetic_ledger VALUES(?,?,?,?,?)",
                           (identity[0], identity[1], identity[3], identity[4], amount))


class Handler(BaseHTTPRequestHandler):
    """处理 HTTP 请求，不在日志里输出请求头、订单、玩家标识或密钥。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def handle(self):
        """进程中断测试会断开保活连接，这里的连接重置属于预期情况。"""
        self.connection.settimeout(10)
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass

    def reply(self, status, value):
        body = json.dumps(value, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        server = self.server
        with server.lock:
            server.requests += 1
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/internal/gamehub" or not 0 < length <= 65536:
                self.close_connection = True
                return self.reply(400, {"error": "路径或请求体长度不对"})
            body = self.rfile.read(length)
            timestamp = self.headers.get("X-Timestamp", "")
            expected = signature(server.secret, timestamp, "POST", self.path, body)
            if abs(int(timestamp) - int(time.time())) > 300 or not hmac.compare_digest(
                    expected, self.headers.get("X-Signature", "")):
                return self.reply(401, {"error": "签名无效"})
            envelope = json.loads(body)
            if envelope["gameId"] != server.game_id or envelope["serverId"] != server.server_id:
                return self.reply(403, {"error": "游戏或区服不匹配"})
            if server.fail:
                return self.reply(503, {"error": "测试中模拟服务不可用"})
            server.commit(envelope)
            if server.pause:
                server.pause.wait(timeout=10)
            if server.drop_once:
                server.drop_once = False
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            return self.reply(200, {"taskId": "wrong" if server.bad_ack else envelope["taskId"], "committed": True})
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            return self.reply(409, {"error": "请求内容冲突或处理失败"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--server-id", type=int, default=1)
    parser.add_argument("--game-id", default="demo")
    parser.add_argument("--database", type=Path, default=Path(".local/demo-receiver.db"))
    args = parser.parse_args()
    secret = os.environ.get("GAMEHUB_DEMO_OUTBOUND_SECRET", "")
    if len(secret) < 32:
        parser.error("请设置 GAMEHUB_DEMO_OUTBOUND_SECRET，长度至少 32 字节")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with DemoReceiver(("127.0.0.1", args.port), args.database, secret, args.game_id, args.server_id) as server:
        print(f"演示接收端已启动：127.0.0.1:{args.port}，请使用测试数据")
        server.serve_forever()


if __name__ == "__main__":
    main()

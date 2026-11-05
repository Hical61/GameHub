"""用假订单演示游戏服接收通知，SQLite 事务保证去重和记账一起提交。"""

import argparse
from contextlib import closing
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


MAX_CONFIG_RESPONSE_BYTES = 256 * 1024


def contains_float(value):
    """未提供规范文本时，只允许各语言可一致编码的非浮点示例内容。"""
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(contains_float(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_float(item) for item in value)
    return False

def signature(secret, timestamp, method, path, body, key_id=""):
    """按 GameHub 演示协议计算 HMAC，签名不含 HTTP 分块帧。"""
    digest = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}\n{method}\n{path}\n{digest}"
    if key_id:
        message += f"\n{key_id}"
    message = message.encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def valid_key_id(value):
    """密钥标识使用与 Hub 一致的有界 ASCII 格式。"""
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_:.-]{1,64}", value)) and value not in (".", "..")


class DemoReceiver(ThreadingHTTPServer):
    """一个实例对应一个游戏区服，故障开关给集成测试用。"""

    daemon_threads = True

    def __init__(self, address, database, secret, game_id="demo", server_id=1, key_id="",
                 previous_keys=None, legacy_secret="", hub_url=None):
        previous_keys = dict(previous_keys or {})
        if len(secret) < 32 or (key_id and not valid_key_id(key_id)):
            raise ValueError("当前密钥或标识无效")
        if (previous_keys or legacy_secret) and not key_id:
            raise ValueError("旧验签窗口需要指定当前密钥标识")
        if len(previous_keys) > 8 or key_id in previous_keys or any(
                not valid_key_id(name) or len(value) < 32 for name, value in previous_keys.items()):
            raise ValueError("旧验签密钥配置无效")
        if legacy_secret and len(legacy_secret) < 32:
            raise ValueError("旧协议密钥长度不够")
        if not valid_key_id(game_id) or len(game_id) > 32 or type(server_id) is not int or not 1 <= server_id <= 2**32 - 1:
            raise ValueError("游戏或区服标识无效")
        self.hub = urlsplit(hub_url) if hub_url else None
        if self.hub and (self.hub.scheme not in ("http", "https") or not self.hub.hostname
                         or self.hub.username or self.hub.password or self.hub.query or self.hub.fragment
                         or self.hub.path not in ("", "/")):
            raise ValueError("Hub URL 必须是可信服务的 HTTP(S) 源站地址")
        if self.hub:
            try:
                ipaddress.ip_address(self.hub.hostname)
                self.hub.port
            except ValueError as error:
                raise ValueError("示例 Hub URL 使用 IP 地址，避免同步 DNS 超出请求总时限") from error
        self.database = str(database)
        self.secret = secret
        self.key_id = key_id
        self.previous_keys = previous_keys
        self.legacy_secret = legacy_secret
        self.game_id = game_id
        self.server_id = server_id
        self.fail = False
        self.bad_ack = False
        self.drop_once = False
        self.pause = None
        self.requests = 0
        self.lock = threading.Lock()
        self.config_stop = threading.Event()
        self.config_thread = None
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox(
                    game_id TEXT, server_id INTEGER, task_type TEXT, namespace_key TEXT, biz_key TEXT,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY(game_id,server_id,task_type,namespace_key,biz_key));
                CREATE TABLE IF NOT EXISTS synthetic_ledger(
                    game_id TEXT, server_id INTEGER, namespace_key TEXT, biz_key TEXT, amount_minor INTEGER,
                    PRIMARY KEY(game_id,server_id,namespace_key,biz_key));
                CREATE TABLE IF NOT EXISTS synthetic_config(
                    game_id TEXT, server_id INTEGER, version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, content TEXT NOT NULL,
                    PRIMARY KEY(game_id,server_id));
            """)
        super().__init__(address, Handler)
        if self.hub:
            self.config_thread = threading.Thread(target=self.sync_config, daemon=True)
            self.config_thread.start()

    def verification_key(self, key_id):
        """按标识唯一选密钥；无标识仅使用显式旧协议密钥，不逐个尝试。"""
        if not key_id:
            return self.legacy_secret if self.key_id else self.secret
        if not self.key_id:
            return ""
        return self.secret if key_id == self.key_id else self.previous_keys.get(key_id, "")

    def authenticate(self, headers, method, path, body):
        """验证时间窗和含密钥标识的完整演示签名，返回是否可信。"""
        timestamp = headers.get("X-Timestamp", "")
        key_id = headers.get("X-Key-Id", "")
        secret = self.verification_key(key_id)
        if not secret or not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12:
            return False
        return abs(int(timestamp) - int(time.time())) <= 300 and hmac.compare_digest(
            signature(secret, timestamp, method, path, body, key_id), headers.get("X-Signature", ""))

    def hub_request(self, method, path, body=b""):
        """向配置中的固定 Hub 发起签名请求；每次最多五秒、256 KiB，不跟随重定向。"""
        if not self.hub:
            raise ValueError("未配置 Hub URL")
        timestamp = str(int(time.time()))
        headers = {"X-Timestamp": timestamp, "Content-Type": "application/json",
                   "X-Signature": signature(self.secret, timestamp, method, path, body, self.key_id)}
        if self.key_id:
            headers["X-Key-Id"] = self.key_id
        connection_type = http.client.HTTPSConnection if self.hub.scheme == "https" else http.client.HTTPConnection
        deadline = time.monotonic() + 5
        connection = connection_type(self.hub.hostname, self.hub.port, timeout=5)
        response = None
        transport_holder = {"socket": None}

        def expire():
            for transport in (connection.sock, transport_holder["socket"]):
                if transport:
                    try:
                        transport.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        expiry = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
        expiry.daemon = True
        expiry.start()
        try:
            connection.request(method, path, body, headers)
            transport = connection.sock
            transport_holder["socket"] = transport
            transport.settimeout(max(0.001, deadline - time.monotonic()))
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("Hub 尚无可用配置或拒绝请求")
            content = bytearray()
            while True:
                if response.isclosed():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Hub 请求超时")
                # HTTPConnection 处理 Connection: close 后会清空 sock；响应文件仍持有同一传输。
                transport.settimeout(remaining)
                chunk = response.read1(min(8192, MAX_CONFIG_RESPONSE_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_CONFIG_RESPONSE_BYTES:
                    raise ValueError("Hub 响应超过大小限制")
            return json.loads(content)
        finally:
            expiry.cancel()
            if response is not None:
                response.close()
            connection.close()

    def config_status(self):
        """返回本区服已持久化应用版本与摘要，不返回配置正文。"""
        with closing(sqlite3.connect(self.database, timeout=5)) as db:
            row = db.execute("SELECT version,content_hash FROM synthetic_config WHERE game_id=? AND server_id=?",
                             (self.game_id, self.server_id)).fetchone()
        return {"version": row[0], "contentHash": row[1]} if row else None

    def apply_config(self, version):
        """拉取并原子持久化合成配置，旧版本不能覆盖新版本；这不是生产游戏的热更新实现。"""
        value = self.hub_request("GET", f"/internal/config/{self.game_id}/{self.server_id}/{version}")
        actual_version = value["version"]
        if type(actual_version) is not int or not 1 <= actual_version <= 2**63 - 1:
            raise ValueError("配置版本无效")
        if version and version != actual_version:
            raise ValueError("拉取结果版本不匹配")
        content = value.get("canonicalContent")
        if content is None:
            if contains_float(value["content"]):
                raise ValueError("浮点配置需要 Hub 提供规范内容文本")
            content = json.dumps(value["content"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if not isinstance(content, str) or json.loads(content) != value["content"]:
            raise ValueError("规范配置文本与内容不一致")
        digest = hashlib.sha256(content.encode()).hexdigest()
        if not hmac.compare_digest(digest, value["sha256"]):
            raise ValueError("配置摘要不匹配")
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT version,content_hash FROM synthetic_config WHERE game_id=? AND server_id=?",
                                 (self.game_id, self.server_id)).fetchone()
            if current and current[0] == actual_version and current[1] != digest:
                raise ValueError("相同配置版本内容冲突")
            if not current or actual_version > current[0]:
                db.execute("INSERT OR REPLACE INTO synthetic_config VALUES(?,?,?,?,?)",
                           (self.game_id, self.server_id, actual_version, digest, content))
        return self.config_status()

    def report_config(self):
        """把实际持久化版本报告给 Hub；失败由通知重试和后台同步恢复。"""
        status = self.config_status()
        if status:
            self.hub_request("POST", f"/internal/config-applied/{self.game_id}/{self.server_id}",
                             json.dumps(status, separators=(",", ":")).encode())

    def sync_config(self):
        """启动后异步补拉最新版并重报；每次网络有界，关闭时可中断重试等待。"""
        while not self.config_stop.is_set():
            try:
                self.apply_config(0)
                self.report_config()
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, http.client.HTTPException):
                pass
            self.config_stop.wait(5)

    def server_close(self):
        """停止配置同步后关闭监听；不等待尚未启动的 serve_forever。"""
        self.config_stop.set()
        super().server_close()
        if self.config_thread:
            self.config_thread.join(timeout=6)

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
            if not server.authenticate(self.headers, "POST", self.path, body):
                return self.reply(401, {"error": "签名无效"})
            envelope = json.loads(body)
            if envelope["gameId"] != server.game_id or envelope["serverId"] != server.server_id:
                return self.reply(403, {"error": "游戏或区服不匹配"})
            if server.fail:
                return self.reply(503, {"error": "测试中模拟服务不可用"})
            if envelope["taskType"] not in ("recharge_notify", "announce", "config_push") or any(
                    type(envelope.get(field, 1)) is not int or envelope.get(field, 1) != 1
                    for field in ("schemaVersion", "payloadVersion", "fingerprintVersion")):
                return self.reply(409, {"error": "任务类型或契约版本不支持"})
            if envelope["taskType"] == "config_push" and server.hub:
                version = envelope["payload"]["version"]
                if type(version) is not int or not 1 <= version <= 2**63 - 1:
                    raise ValueError("通知版本无效")
                server.apply_config(version)
            server.commit(envelope)
            if envelope["taskType"] == "config_push" and server.hub:
                server.report_config()
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
        except (OSError, http.client.HTTPException):
            return self.reply(503, {"error": "配置同步依赖暂时不可用"})

    def do_GET(self):
        """签名读取本区服实际应用状态，不公开配置正文。"""
        if self.path != "/internal/config-status":
            return self.reply(404, {"error": "接口不存在"})
        if not self.server.authenticate(self.headers, "GET", self.path, b""):
            return self.reply(401, {"error": "签名无效"})
        try:
            return self.reply(200, {"applied": self.server.config_status()})
        except sqlite3.Error:
            return self.reply(503, {"error": "应用状态暂时不可用"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--server-id", type=int, default=1)
    parser.add_argument("--game-id", default="demo")
    parser.add_argument("--database", type=Path, default=Path(".local/demo-receiver.db"))
    parser.add_argument("--key-id", default="", help="当前出向密钥标识；不指定时保留旧协议")
    parser.add_argument("--secret-env", default="GAMEHUB_DEMO_OUTBOUND_SECRET")
    parser.add_argument("--previous-key", action="append", default=[], metavar="KEYID=ENV",
                        help="旧标识和密钥环境变量，可重复使用，最多八项")
    parser.add_argument("--legacy-secret-env", help="启用轮换后，允许无标识请求使用的独立旧密钥环境变量")
    parser.add_argument("--hub-url", help="可信内网 Hub 源站，启用合成配置应用、启动补拉和版本回报")
    args = parser.parse_args()
    secret = os.environ.get(args.secret_env, "")
    if len(secret) < 32:
        parser.error("请设置当前密钥环境变量，长度至少 32 字节")
    previous_keys = {}
    for value in args.previous_key:
        key_id, separator, environment = value.partition("=")
        if not separator or key_id in previous_keys or not environment:
            parser.error("旧密钥使用不重复的 KEYID=ENV 格式")
        previous_keys[key_id] = os.environ.get(environment, "")
    legacy_secret = os.environ.get(args.legacy_secret_env, "") if args.legacy_secret_env else ""
    if args.legacy_secret_env and len(legacy_secret) < 32:
        parser.error("旧协议密钥环境变量未设置或长度不够")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    try:
        server = DemoReceiver(("127.0.0.1", args.port), args.database, secret, args.game_id, args.server_id,
                              args.key_id, previous_keys, legacy_secret, args.hub_url)
    except ValueError as error:
        parser.error(str(error))
    with server:
        print(f"演示接收端已启动：127.0.0.1:{args.port}，请使用测试数据")
        server.serve_forever()


if __name__ == "__main__":
    main()

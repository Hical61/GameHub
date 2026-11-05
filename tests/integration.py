"""在独立临时 MySQL 和模拟游戏服中检查故障恢复与多游戏隔离。"""
import argparse
import concurrent.futures
from contextlib import closing
from collections import deque
from contextvars import ContextVar
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "scripts"))
from demo_receiver import DemoReceiver, signature
from admin_token import encode, token
from deadlock_observation import assert_no_new_deadlocks
from integration_errors import IntegrationDeadlineExceeded


ACTIVE_DEADLINE = ContextVar("gamehub_integration_deadline", default=None)
HTTP_WRITE_CHUNK_BYTES = 16 * 1024


class RunDeadline:
    """测试轮询和 I/O 共用一个截止时间，不异步打断进程创建与登记。"""

    def __init__(self, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("测试总时限必须是正数")
        self.seconds = seconds
        self.deadline = None
        self.token = None

    def __enter__(self):
        self.deadline = time.monotonic() + self.seconds
        self.token = ACTIVE_DEADLINE.set(self)
        return self

    def timeout(self, maximum):
        """返回不超过整轮剩余时间的单次等待上限；超时后抛固定错误。"""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise IntegrationDeadlineExceeded("集成测试超过总时限，开始保存诊断并清理")
        return min(maximum, remaining)

    def __exit__(self, _kind, error, _traceback):
        ACTIVE_DEADLINE.reset(self.token)
        if error is None:
            self.timeout(self.seconds)


class HttpDeadline:
    """一次 HTTP 操作共用绝对截止点；整轮时限还可跨线程共享。"""

    def __init__(self, maximum, run_deadline=None):
        """maximum 为本次操作的秒数上限，run_deadline 为可选的整轮截止点。"""
        self.deadline = time.monotonic() + maximum
        self.run_deadline = run_deadline

    def timeout(self):
        """返回本次与整轮共同剩余的秒数；到期时抛相应异常。"""
        remaining = self.deadline - time.monotonic()
        if self.run_deadline:
            remaining = self.run_deadline.timeout(remaining)
        if remaining <= 0:
            raise TimeoutError("HTTP 操作超过单次时限")
        return remaining

    def raise_timeout(self):
        """底层计时器触发时按限制来源报错，避免 Windows 毫秒取整导致整轮超时被误判。"""
        if self.run_deadline:
            self.run_deadline.timeout(0)
            if self.run_deadline.deadline <= self.deadline:
                raise IntegrationDeadlineExceeded("集成测试超过总时限，开始保存诊断并清理") from None
        raise TimeoutError("HTTP 操作超过单次时限") from None

    def connect(self, port):
        """连接本机回环 port，返回所有读写共用本截止点的 socket。"""
        connection = DeadlineSocket(self)
        try:
            connection.connect(("127.0.0.1", port))
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return connection
        except BaseException:
            connection.close()
            raise


class DeadlineSocket(socket.socket):
    """让 HTTPResponse 的缓冲读取也在每次底层 I/O 前重新检查绝对截止点。"""

    def __init__(self, deadline):
        """deadline 控制连接、发送与读取；socket 的关闭仍遵循标准 makefile 生命周期。"""
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self.deadline = deadline

    def _io(self, action, *arguments):
        super().settimeout(self.deadline.timeout())
        try:
            result = action(*arguments)
        except TimeoutError:
            self.deadline.raise_timeout()
        except OSError:
            # socket 超时、EOF 前的重置都不能把整轮到期转成普通可重试网络错误。
            self.deadline.timeout()
            raise
        self.deadline.timeout()
        return result

    def connect(self, address):
        """连接 address，无返回值；连接时间计入同一个绝对预算。"""
        return self._io(super().connect, address)

    def sendall(self, data, flags=0):
        """完整发送 data，无返回值；部分发送不会重置预算。"""
        pending = memoryview(data)
        while pending:
            sent = self._io(super().send, pending[:HTTP_WRITE_CHUNK_BYTES], flags)
            if not sent:
                raise ConnectionError("HTTP 连接在发送时关闭")
            pending = pending[sent:]

    def recv_into(self, buffer, nbytes=0, flags=0):
        """向 buffer 读取最多 nbytes 字节，返回读取量；供 makefile 的缓冲层调用。"""
        return self._io(super().recv_into, buffer, nbytes, flags)


def free_port():
    """取一个空闲回环端口；实际操作数据库时还会核对本次数据目录。"""
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def wait_for(predicate, description, timeout=20):
    """反复检查条件，超时后给出正在等哪一步。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = ACTIVE_DEADLINE.get()
        if active:
            active.timeout(timeout)
        if predicate():
            return
        time.sleep(active.timeout(0.05) if active else 0.05)
    raise AssertionError("等待超时：" + description)


class Fixture:
    def __init__(self, binary, mysql_bin):
        """binary 指向待测程序，mysql_bin 指向 MySQL 工具目录；创建本次专用目录。"""
        self.binary = binary.resolve()
        self.mysql_bin = mysql_bin.resolve()
        self.base = (ROOT / ".local").resolve()
        self.base.mkdir(exist_ok=True)
        # 普通 mkdir 会继承工作区权限，兼容 Windows 下的受限运行账号。
        self.path = self.base / ("integration-" + uuid.uuid4().hex)
        self.path.mkdir()
        self.expected_datadir = self.path / "mysql-data"
        self.db_port = free_port()
        self.http_port = free_port()
        self.env = dict(os.environ)
        for name in ("GAMEHUB_HUBDB_PASSWORD", "GAMEHUB_ADMIN_JWT_SECRET", "GAMEHUB_DEMO_CALLBACK_SECRET", "GAMEHUB_DEMO_OUTBOUND_SECRET"):
            self.env[name] = secrets.token_hex(24)
        self.children = []
        self.database_children = set()
        self.logs = []
        self.receivers = []
        self.receiver_threads = {}
        self.hub = None
        self.responses = deque(maxlen=20)
        self.response_lock = threading.Lock()
        self.sql_errors = deque(maxlen=20)
        self.sql_error_lock = threading.Lock()
        self.diagnostic_path = None
        self.run_deadline = None

    def operation_timeout(self, maximum):
        """单次等待最多 maximum 秒；正常测试还要服从总时限，清理使用自己的预算。"""
        return self.run_deadline.timeout(maximum) if self.run_deadline else maximum

    def http_deadline(self, maximum=10):
        """返回单次 HTTP 预算；优先使用 Fixture 上可被线程池共享的整轮截止点。"""
        return HttpDeadline(maximum, self.run_deadline or ACTIVE_DEADLINE.get())

    def launch(self, command, name, **kwargs):
        self.operation_timeout(10)
        log = (self.path / (name + ".log")).open("wb")
        self.logs.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=self.env, **kwargs)
        self.children.append(process)
        if name in {"mysql", "mysql-init"}:
            self.database_children.add(process)
        # 即使创建进程花掉了剩余时间，也先登记再报超时，清理才能找到它。
        self.operation_timeout(10)
        return process

    def sql(self, sql, timeout=10):
        """在专用实例执行 sql，最多等待 timeout 秒，返回文本结果；先检查数据库身份。"""
        # 同一个会话里先检查 datadir，再执行 SQL，防止端口被别的实例占用。
        expected = self.expected_datadir.as_posix().rstrip("/").replace("'", "''")
        actual = "TRIM(TRAILING '/' FROM REPLACE(@@datadir,CHAR(92),'/'))"
        compare = f"LOWER({actual})=LOWER('{expected}')" if os.name == "nt" else f"{actual}='{expected}'"
        guard = f"SET @gamehub_guard_sql=IF({compare},'DO 0','INVALID GAMEHUB DATABASE IDENTITY'); "
        guard += "PREPARE gamehub_guard FROM @gamehub_guard_sql; EXECUTE gamehub_guard; DEALLOCATE PREPARE gamehub_guard;\n"
        result = subprocess.run([str(self.mysql_bin / "mysql.exe" if os.name == "nt" else self.mysql_bin / "mysql"),
            "--no-defaults", "--default-character-set=utf8mb4", "--protocol=TCP", "-h127.0.0.1",
            f"-P{self.db_port}", "-uroot", "--batch", "--skip-column-names"],
            input=(guard + sql).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=self.operation_timeout(timeout), env=self.env)
        if result.returncode:
            # 只取 mysql 固定前缀中的数字/SQLSTATE/行号，绝不保存后面的 SQL 或参数。
            pattern = rb"(?m)^ERROR (\d+)(?: \(([0-9A-Z]{5})\))?(?: at line (\d+))?:"
            with self.sql_error_lock:
                for match in re.finditer(pattern, result.stderr):
                    self.sql_errors.append({"code": int(match[1]),
                        "sqlState": match[2].decode("ascii") if match[2] else None,
                        "line": int(match[3]) if match[3] else None})
            raise RuntimeError("隔离测试库 SQL 执行失败")
        return result.stdout.decode("utf-8").strip()

    def start(self):
        data = self.path / "mysql-data"
        mysqld = self.mysql_bin / ("mysqld.exe" if os.name == "nt" else "mysqld")
        self.launch([str(mysqld), "--no-defaults", "--initialize-insecure", f"--datadir={data}",
            "--log-error-verbosity=1"], "mysql-init").wait(timeout=self.operation_timeout(120))
        if self.children[-1].returncode:
            raise RuntimeError("隔离 MySQL 初始化失败")
        command = [str(mysqld), "--no-defaults", f"--datadir={data}", "--bind-address=127.0.0.1",
            f"--port={self.db_port}", "--mysqlx=0", "--skip-log-bin", "--performance-schema=OFF",
            "--innodb-buffer-pool-size=32M", "--max-connections=30", f"--secure-file-priv={self.path}"]
        self.db = self.launch(command, "mysql")

        def database_ready():
            if self.db.poll() is not None:
                raise RuntimeError("隔离 MySQL 启动失败")
            try:
                return self.sql("SELECT 1") == "1"
            except (RuntimeError, subprocess.TimeoutExpired):
                return False

        wait_for(database_ready, "MySQL 就绪", 45)
        migrations = sorted((ROOT / "migrations").glob("*.sql"))
        if not migrations:
            raise RuntimeError("没有找到数据库迁移脚本")
        self.sql("CREATE DATABASE gamehub CHARACTER SET utf8mb4")
        for migration in migrations:
            self.sql("USE gamehub;\n" + migration.read_text(encoding="utf-8"))
        password = self.env["GAMEHUB_HUBDB_PASSWORD"]
        self.sql(f"CREATE USER 'gamehub'@'localhost' IDENTIFIED BY '{password}'; "
                 "GRANT SELECT,INSERT,UPDATE,DELETE ON gamehub.* TO 'gamehub'@'localhost';")
        config = json.loads((ROOT / "config/gamehub.example.json").read_text(encoding="utf-8"))
        config["server"]["port"] = self.http_port
        config["hubDb"]["port"] = self.db_port
        config["worker"].update(pollMs=50, concurrency=3, maxAttempts=3, baseRetrySeconds=1,
                                maxRetrySeconds=2, leaseSeconds=30, connectTimeoutMs=500, requestTimeoutMs=1000)
        config["games"][0]["servers"] = []
        for server_id in (1, 2):
            receiver = DemoReceiver(("127.0.0.1", 0), self.path / f"receiver-{server_id}.db",
                self.env["GAMEHUB_DEMO_OUTBOUND_SECRET"], server_id=server_id)
            self._start_receiver(receiver)
            config["games"][0]["servers"].append({"id": server_id, "host": "127.0.0.1",
                "port": receiver.server_port, "target": "/internal/gamehub"})
        self.config = self.path / "gamehub.json"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.check_configs(config)
        storage = self.binary.with_name("gamehub_storage_tests" + self.binary.suffix)
        # 仅本次独立临时实例的存储测试需要观察 root definer 触发器内的连接。
        # 退出存储测试后先撤销 PROCESS，再以原有四项 DML 权限启动 Hub。
        self.sql("GRANT PROCESS ON *.* TO 'gamehub'@'localhost'")
        # 仅故障注入测试需要在这两张隔离表创建触发器，运行服务前全部撤销。
        self.sql("GRANT TRIGGER ON gamehub.delivery_attempt TO 'gamehub'@'localhost'; "
                 "GRANT TRIGGER ON gamehub.audit_log TO 'gamehub'@'localhost'")
        try:
            result = subprocess.run([str(storage), str(self.config)], capture_output=True, env=self.env,
                                    timeout=self.operation_timeout(30))
        finally:
            self.sql("REVOKE TRIGGER ON gamehub.delivery_attempt FROM 'gamehub'@'localhost'; "
                     "REVOKE TRIGGER ON gamehub.audit_log FROM 'gamehub'@'localhost'")
            self.sql("REVOKE PROCESS ON *.* FROM 'gamehub'@'localhost'")
        if result.returncode:
            print(result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"))
            raise AssertionError("存储集成测试失败")
        print("通过：真实 MySQL 租约隔离、过期确认、最终尝试回收与异常回滚")
        self.start_hub()

    def _start_receiver(self, receiver):
        thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        self.receivers.append(receiver)
        self.receiver_threads[receiver] = thread
        thread.start()

    def check_configs(self, valid):
        """检查启动前能否拒绝错误配置，并确认日志没有泄露密钥。"""
        variants = [(valid, True)]
        for address in ("127.0.0.1", "10.23.45.67", "::1", "0:0:0:0:0:0:0:1", "fd00::1", "::ffff:127.0.0.1"):
            valid_address = json.loads(json.dumps(valid))
            valid_address["server"]["listen"] = address
            variants.append((valid_address, True))
        for address in ("0.0.0.0", "::", "0::", "0:0:0:0:0:0:0:0",
                        "0000:0000:0000:0000:0000:0000:0000:0000", "::0.0.0.0", "::%0",
                        "::ffff:0.0.0.0", "0:0:0:0:0:ffff:0:0", "::FFFF:0000:0000",
                        "localhost", "127.0.0.999", "2001:db8:::1", "1:2:3:4:5:6:7:8:9"):
            bad_address = json.loads(json.dumps(valid))
            bad_address["server"]["listen"] = address
            variants.append((bad_address, False))
        for game_id, accepted in ((".", False), ("..", False), ("demo.alpha", True), ("demo..beta", True)):
            game_identifier = json.loads(json.dumps(valid))
            game_identifier["games"][0]["id"] = game_id
            variants.append((game_identifier, accepted))
        duplicate_game = json.loads(json.dumps(valid))
        duplicate_game["games"].append(duplicate_game["games"][0])
        variants.append((duplicate_game, False))
        duplicate_server = json.loads(json.dumps(valid))
        duplicate_server["games"][0]["servers"].append(duplicate_server["games"][0]["servers"][0])
        variants.append((duplicate_server, False))
        short_lease = json.loads(json.dumps(valid))
        short_lease["worker"]["leaseSeconds"] = 10
        variants.append((short_lease, False))
        unknown_adapter = json.loads(json.dumps(valid))
        unknown_adapter["games"][0]["adapter"] = "unknown"
        variants.append((unknown_adapter, False))
        for index, (variant, accepted) in enumerate(variants):
            path = self.path / f"config-check-{index}.json"
            path.write_text(json.dumps(variant), encoding="utf-8")
            result = subprocess.run([str(self.binary), "--config", str(path), "--check-config"],
                capture_output=True, env=self.env, timeout=self.operation_timeout(10))
            assert (result.returncode == 0) == accepted, f"配置检查结果错误：变体 {index}"
            output = result.stdout + result.stderr
            assert all(self.env[name].encode() not in output for name in (
                "GAMEHUB_HUBDB_PASSWORD", "GAMEHUB_ADMIN_JWT_SECRET", "GAMEHUB_DEMO_CALLBACK_SECRET", "GAMEHUB_DEMO_OUTBOUND_SECRET"))
        print("通过：通配及非法监听地址、重复游戏、重复区服、短租约及未知适配器拒绝，合法 IP 检查通过")

    def start_hub(self):
        self.hub = self.launch([str(self.binary), "--config", str(self.config)], f"hub-{len(self.children)}")
        def ready():
            if self.hub.poll() is not None:
                raise RuntimeError("GameHub 进程提前退出")
            try:
                return self.request("GET", "/admin/ready")[0] == 200
            except OSError:
                return False
        wait_for(ready, "GameHub 就绪")

    def request(self, method, path, value=None, admin=None, signed=False, chunked=False, raw=None, key_id="", secret_env=None):
        body = raw if raw is not None else (json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode() if value is not None else b"")
        headers = {"Content-Type": "application/json"}
        if admin:
            headers["Authorization"] = "Bearer " + admin
        if signed:
            timestamp = str(int(time.time()))
            secret = self.env[secret_env or ("GAMEHUB_DEMO_CALLBACK_SECRET" if path.startswith("/api/callback/") else "GAMEHUB_DEMO_OUTBOUND_SECRET")]
            headers.update({"X-Timestamp": timestamp, "X-Signature": signature(secret, timestamp, method, path, body, key_id)})
            if key_id:
                headers["X-Key-Id"] = key_id
        deadline = self.http_deadline()
        with closing(http.client.HTTPConnection("127.0.0.1", self.http_port,
                                               timeout=deadline.timeout())) as connection:
            connection.sock = deadline.connect(self.http_port)
            connection.request(method, path, [body[:10], body[10:]] if chunked else body, headers, encode_chunked=chunked)
            deadline.timeout()
            with closing(connection.getresponse()) as result:
                deadline.timeout()
                content = result.read()
                deadline.timeout()
                with self.response_lock:
                    self.responses.append({"method": method, "path": path, "status": result.status, "bodyBytes": len(content)})
                if not content:
                    return result.status, {}
                if result.getheader("Content-Type", "").startswith("application/json"):
                    return result.status, json.loads(content)
                return result.status, {"message": content.decode(errors="replace")}

    def admin(self, role="operator", games=None):
        return token(self.env["GAMEHUB_ADMIN_JWT_SECRET"], "synthetic-operator", role, games or ["*"])

    def callback_burst(self, path, count=1024):
        """在同一连接里一次发完请求，确保请求速度足以触发限流。"""
        prefix = f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\nConnection: "
        keep_alive = (prefix + "keep-alive\r\n\r\n").encode("ascii")
        close = (prefix + "close\r\n\r\n").encode("ascii")
        statuses = []
        started = time.monotonic()
        deadline = self.http_deadline()
        with deadline.connect(self.http_port) as connection:
            connection.sendall(keep_alive * (count - 1) + close)
            # 共用读取缓冲，免得预读到下一条响应后把它丢掉。
            with connection.makefile("rb") as stream:
                for _ in range(count):
                    deadline.timeout()
                    status_line = stream.readline().split(b" ", 2)
                    assert len(status_line) == 3 and status_line[0] == b"HTTP/1.1", "洪峰响应不完整"
                    headers = http.client.parse_headers(stream)
                    assert not headers.get("Transfer-Encoding"), "洪峰测试预期固定长度响应"
                    length = int(headers["Content-Length"])
                    assert length >= 0 and len(stream.read(length)) == length, "洪峰响应实体不完整"
                    statuses.append(int(status_line[1]))
                    if headers.get("Connection", "").lower() == "close":
                        try:
                            remaining = stream.read(1)
                        except ConnectionResetError:
                            # 拒绝请求后关闭尚有未读数据的连接，也可以表现为 TCP reset。
                            remaining = b""
                        assert remaining == b"", "连接关闭响应之后仍出现额外响应"
                        break
                    assert statuses[-1] != 429, "前置限流拒绝后仍复用未消费的连接"
                else:
                    raise AssertionError("洪峰请求未按约定结束连接")
        deadline.timeout()
        elapsed = time.monotonic() - started
        assert set(statuses) <= {401, 429}, f"洪峰出现异常状态：{set(statuses)}"
        assert statuses[-1] == 429, f"未触发限流：{count} 个请求耗时 {elapsed:.3f} 秒"
        print(f"限流检查：写入 {count} 个流水请求，接收 {len(statuses)} 条响应后 429 关闭连接，耗时 {elapsed:.3f} 秒")

    def tasks(self, event_id):
        value = self.sql(f"SELECT status FROM gamehub.delivery_task WHERE event_id={int(event_id)} ORDER BY server_id")
        return [int(item) for item in value.splitlines()] if value else []

    def ledger(self, server_id, biz_key):
        with closing(sqlite3.connect(self.receivers[server_id - 1].database)) as db:
            return db.execute("SELECT COUNT(*) FROM synthetic_ledger WHERE biz_key=?", (biz_key,)).fetchone()[0]

    def _redact(self, text):
        for name in ("GAMEHUB_HUBDB_PASSWORD", "GAMEHUB_ADMIN_JWT_SECRET",
                     "GAMEHUB_DEMO_CALLBACK_SECRET", "GAMEHUB_DEMO_OUTBOUND_SECRET"):
            text = text.replace(self.env[name], "[已隐藏]")
        return re.sub(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[已隐藏令牌]", text)

    def _diagnostic_value(self, action):
        try:
            return action()
        except Exception as error:
            return {"errorType": type(error).__name__}

    def save_diagnostics(self, failure):
        """在停进程前记录失败状态；failure 只记录类型和位置，返回脱敏诊断目录。"""
        self.diagnostic_path = self.base / (self.path.name + "-diagnostics")
        self.diagnostic_path.mkdir(exist_ok=True)
        with self.sql_error_lock:
            sql_errors = list(self.sql_errors)
        report = {
            "capturedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "failureType": type(failure).__name__,
            "failureFrames": [{"file": Path(frame.filename).name, "line": frame.lineno,
                               "function": frame.name} for frame in traceback.extract_tb(failure.__traceback__)],
            "hubExitCode": self.hub.poll() if self.hub else None,
            "recentResponses": list(self.responses),
            "sqlErrors": sql_errors,
            "ready": self._diagnostic_value(self._ready_snapshot),
            "tasks": self._diagnostic_value(lambda: self.sql(
                "SELECT id,event_id,game_id,server_id,status,attempts,generation,last_error,"
                "next_retry_at,lease_until,created_at,updated_at,UTC_TIMESTAMP(6) "
                "FROM gamehub.delivery_task ORDER BY id DESC LIMIT 1000")),
            # 不保存 SQL 原文；进程列表里的查询可能含密码或请求参数。
            "databaseProcesses": self._diagnostic_value(lambda: self.sql(
                "SELECT ID,COMMAND,TIME,STATE FROM information_schema.PROCESSLIST ORDER BY ID")),
            "databaseTransactions": self._diagnostic_value(lambda: self.sql(
                "SELECT trx_id,trx_state,trx_started,trx_wait_started,trx_mysql_thread_id,"
                "trx_rows_locked,trx_rows_modified FROM information_schema.innodb_trx")),
            "receivers": [],
        }
        for receiver in self.receivers:
            with receiver.lock:
                report["receivers"].append({"serverId": receiver.server_id, "requests": receiver.requests})
        text = self._redact(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        (self.diagnostic_path / "diagnostics.json").write_text(text, encoding="utf-8", newline="\n")
        return self.diagnostic_path

    def _ready_snapshot(self):
        # 清理时使用独立预算，不能继承已经到期的测试时限。
        deadline = HttpDeadline(1)
        with closing(http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=1)) as connection:
            connection.sock = deadline.connect(self.http_port)
            connection.request("GET", "/admin/ready")
            with closing(connection.getresponse()) as response:
                deadline.timeout()
                return {"status": response.status}

    def _save_logs(self):
        for log in self.logs:
            if not log.closed:
                log.flush()
        # 最多保留末尾 512 KiB，只留完整行，避免截断后漏掉半个密钥或令牌。
        max_bytes = 512 * 1024
        for source in self.path.glob("*.log"):
            start = max(0, source.stat().st_size - max_bytes)
            with source.open("rb") as stream:
                stream.seek(max(0, start - 1))
                previous = stream.read(1) if start else b"\n"
                data = stream.read(max_bytes)
            if previous != b"\n":
                data = data.partition(b"\n")[2]
            if not data.endswith(b"\n"):
                data = data.rpartition(b"\n")[0] + b"\n" if b"\n" in data else b""
            text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            (self.diagnostic_path / source.name).write_text(self._redact(text), encoding="utf-8", newline="\n")

    def _stop_child(self, child):
        if child.poll() is not None:
            return
        try:
            if os.name == "nt" and child in self.database_children:
                # mysqld 的 Windows 监控进程可能另起子进程；只结束本次 Popen 的进程树。
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            else:
                child.terminate()
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def _remove_workdir(self):
        path = self.path.resolve()
        if path.parent != self.base or not path.name.startswith("integration-"):
            raise RuntimeError("临时目录不在测试目录内，已跳过删除")
        for retry in range(30):
            try:
                shutil.rmtree(path)
                return
            except PermissionError:
                if retry == 29:
                    raise
                time.sleep(0.1)

    def close(self, failure=None):
        """failure 为原测试异常；失败时先留诊断，再停止进程并清理，无返回值。"""
        errors = []
        def attempt(action):
            try:
                action()
                return True
            except Exception as error:
                errors.append(type(error).__name__)
                return False

        diagnostics_saved = failure is None
        if failure is not None:
            diagnostics_saved = attempt(lambda: self.save_diagnostics(failure))
            if diagnostics_saved:
                diagnostics_saved = attempt(self._save_logs)
        if self.hub:
            attempt(lambda: self._stop_child(self.hub))
        for receiver in self.receivers:
            if receiver.pause:
                attempt(receiver.pause.set)
            thread = self.receiver_threads.get(receiver)
            # shutdown 会等 serve_forever 退出；从未启动线程时调用会一直等下去。
            if thread and thread.is_alive():
                attempt(receiver.shutdown)
            attempt(receiver.server_close)
        # 同会话检查 datadir 后再 SHUTDOWN，避免端口被抢占时关错数据库。
        if getattr(self, "db", None) and self.db.poll() is None:
            try:
                self.sql("SHUTDOWN")
                self.db.wait(timeout=15)
            except Exception:
                pass
        for child in reversed(self.children):
            attempt(lambda child=child: self._stop_child(child))
        if failure is not None and self.diagnostic_path:
            # 再取一次日志，把进程退出时写出的内容也保存下来。
            diagnostics_saved = attempt(self._save_logs) and diagnostics_saved
        for log in self.logs:
            attempt(log.close)
        all_stopped = all(child.poll() is not None for child in self.children)
        if diagnostics_saved and all_stopped:
            attempt(self._remove_workdir)
        else:
            errors.append("临时目录保留")
        if failure is not None:
            if diagnostics_saved:
                print("测试失败，诊断和日志已保存到：" + str(self.diagnostic_path))
            else:
                print("诊断保存不完整，原临时目录仍保留在：" + str(self.path))
        if errors:
            print("测试清理未全部完成：" + ", ".join(errors), file=sys.stderr)
            if failure is None:
                raise RuntimeError("测试清理未全部完成")


class MultiGameFixture(Fixture):
    """复用完整建库和清理流程，在第一次启动前增加两款合成游戏。"""

    def start_hub(self):
        """首次追加合成游戏配置，以后重启沿用测试写入的范围与暂停设置。"""
        if not getattr(self, "multi_configured", False):
            config = json.loads(self.config.read_text(encoding="utf-8"))
            config["worker"].update(concurrency=4, requestTimeoutMs=3000)
            config["games"][0].update(maxConcurrentDeliveries=2, maxConcurrentPerServer=1)
            for game_id, adapter in (("other", "demo-hmac-v1"), ("sample_beta", "demo-hmac-v1")):
                receiver = DemoReceiver(("127.0.0.1", 0), self.path / f"{game_id}-receiver.db",
                                        self.env["GAMEHUB_DEMO_OUTBOUND_SECRET"], game_id=game_id)
                self._start_receiver(receiver)
                game = {"id": game_id, "adapter": adapter,
                        "callbackSecretEnv": "GAMEHUB_DEMO_CALLBACK_SECRET",
                        "outboundSecretEnv": "GAMEHUB_DEMO_OUTBOUND_SECRET",
                        "maxConcurrentDeliveries": 1, "maxConcurrentPerServer": 1,
                        "servers": [{"id": 1, "host": "127.0.0.1", "port": receiver.server_port,
                                     "target": "/internal/gamehub"}]}
                if game_id == "other":
                    game.update(callbackRatePerSecond=1, callbackBurst=10, channels=[
                        {"id": "merchant_a", "adapter": "demo-hmac-v1", "namespace": "merchant_a",
                         "secretEnv": "GAMEHUB_DEMO_CALLBACK_SECRET"},
                        {"id": "merchant_b", "adapter": "demo-hmac-v1", "namespace": "merchant_b",
                         "secretEnv": "GAMEHUB_DEMO_CALLBACK_SECRET"}])
                config["games"].append(game)
            # 多于单次领取检查预算的空游戏，验证轮转跨 tick 仍能到达有任务游戏。
            for number in range(5):
                idle = json.loads(json.dumps(config["games"][0]))
                idle.update(id=f"idle_{number}", maxConcurrentDeliveries=1)
                config["games"].insert(1, idle)
            self.config.write_text(json.dumps(config), encoding="utf-8")
            self.multi_configured = True
        super().start_hub()

    def configure_worker(self, games, enabled=True):
        """停止本测试创建的 Hub，修改领取范围后重启；数据库和任务保留。"""
        self.hub.terminate()
        self.hub.wait(timeout=self.operation_timeout(10))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["worker"]["games"] = games
        config["games"][0]["deliveryEnabled"] = enabled
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.start_hub()



def order(key, server_id=1):
    return {"bizKey": key, "channelOrderId": key, "serverId": server_id, "playerId": "synthetic-player",
            "productId": "synthetic-product", "amountMinor": 100, "currency": "CNY"}


def verify_path_boundaries(f):
    """检查非规范路径返回 400，并且不会继续处理该连接上的后续请求。"""
    paths = (
        ("POST", "/api/%63allback/demo/demo"),
        ("POST", "/api%2Fcallback/demo/demo"),
        ("POST", "/api/callback/d%65mo/demo"),
        ("POST", "/api/callback/demo/%00demo"),
        ("POST", "/api/callback/demo/de+mo"),
        ("GET", "/%61dmin/health"),
        ("GET", "/internal/%63onfig/demo/1/0"),
    )
    cases = [(method, path, b"{}") for method, path in paths]
    # 没有匹配路由、也没有请求体的情况同样要测。
    for path in ("/admin/../api/callback/demo/demo", "//api/callback/demo/demo",
                 "/api/./callback/demo/demo", "/api/callback/demo/demo/.",
                 "/api/callback/demo/demo/..", "/api\\callback/demo/demo"):
        cases.append(("POST", path, b""))
    for method, path, body in cases:
        # 把请求体和下一条请求一起发出，检查拒绝后是否关闭连接。
        rejected = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n").encode("ascii") + body
        following = b"GET /admin/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        deadline = f.http_deadline()
        with deadline.connect(f.http_port) as connection:
            connection.sendall(rejected + following)
            with connection.makefile("rb") as stream:
                status = stream.readline().split(b" ", 2)
                assert len(status) == 3 and status[0] == b"HTTP/1.1" and status[1] == b"400", path
                headers = http.client.parse_headers(stream)
                assert headers.get("Connection", "").lower() == "close", path
                assert not headers.get("Transfer-Encoding"), path
                length = int(headers["Content-Length"])
                assert length >= 0 and len(stream.read(length)) == length, path
                try:
                    remaining = stream.read(1)
                except ConnectionResetError:
                    remaining = b""
                assert remaining == b"", "拒绝非规范路径后仍处理了流水请求"
        deadline.timeout()
    assert f.request("GET", "/internal/config/demo/1/0", signed=True)[0] == 200
    assert f.request("GET", "/admin/health")[0] == 200
    print("通过：非规范路径分桶前拒绝并关闭连接，合法配置拉取与探针仍可用")


def verify_retry_limit(f):
    """重启时降低重试上限，检查旧任务能否转为失败并重新投递。"""
    f.hub.terminate()
    f.hub.wait(timeout=10)
    config = json.loads(f.config.read_text(encoding="utf-8"))
    config["worker"].update(maxAttempts=8, baseRetrySeconds=10, maxRetrySeconds=10)
    f.config.write_text(json.dumps(config), encoding="utf-8")
    f.start_hub()
    f.receivers[0].fail = True
    status, accepted = f.request("POST", "/api/callback/demo/demo", order("lower-retry-limit"), signed=True)
    assert status == 200
    event_id = int(accepted["eventId"])
    state_sql = f"SELECT CONCAT(status,':',attempts) FROM gamehub.delivery_task WHERE event_id={event_id}"
    wait_for(lambda: f.sql(state_sql) == "0:1", "旧上限下第一次失败后 pending")
    f.hub.terminate()
    f.hub.wait(timeout=10)
    config["worker"]["maxAttempts"] = 1
    f.config.write_text(json.dumps(config), encoding="utf-8")
    f.receivers[0].fail = False
    f.start_hub()
    wait_for(lambda: f.sql(state_sql) == "3:1", "下调上限后旧任务到期转 failed")
    assert f.ledger(1, "lower-retry-limit") == 0
    task_id = f.sql(f"SELECT id FROM gamehub.delivery_task WHERE event_id={event_id}")
    assert f.request("POST", f"/admin/tasks/demo/{task_id}/replay", admin=f.admin())[0] == 200
    wait_for(lambda: f.tasks(event_id) == [2], "下调上限后的失败任务重排完成")
    assert f.ledger(1, "lower-retry-limit") == 1
    print("通过：重试上限从 8 降为 1，旧 pending 任务到期后失败，重排后完成")


def verify(fixture):
    f = fixture
    callback = "/api/callback/demo/demo"
    operator = f.admin()
    viewer = f.admin("viewer", ["demo"])
    original_datadir = f.expected_datadir
    f.expected_datadir = f.path / "identity-mismatch"
    try:
        try:
            f.sql("CREATE DATABASE guard_must_not_exist")
            raise AssertionError("错误数据库身份未阻止 DDL")
        except RuntimeError:
            pass
    finally:
        f.expected_datadir = original_datadir
    assert f.sql("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='guard_must_not_exist'") == "0"
    print("通过：数据库实例身份在同会话校验，端口碰撞不能触发误写")
    assert f.request("POST", callback, order("http-dedupe"))[0] == 401
    def concurrent_callback(_):
        # 请求有总超时，繁忙时可能返回 503；重试时保留原订单号。
        for attempt in range(5):
            result = f.request("POST", callback, order("http-dedupe"), signed=True)
            if result[0] != 503:
                return result
            time.sleep(0.05 * (attempt + 1))
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(concurrent_callback, range(10)))
    assert all(status == 200 for status, _ in results), [status for status, _ in results]
    assert len({body["eventId"] for _, body in results}) == 1
    event_id = results[0][1]["eventId"]
    wait_for(lambda: f.tasks(event_id) == [2], "重复回调投递完成")
    assert f.ledger(1, "http-dedupe") == 1
    with assert_no_new_deadlocks(f), concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for _ in range(10):
            round_results = list(pool.map(lambda _: f.request("POST", callback, order("http-dedupe"), signed=True), range(10)))
            assert all(status == 200 for status, _ in round_results), [status for status, _ in round_results]
            assert all(body["eventId"] == event_id and body["duplicate"] for _, body in round_results)
    assert f.ledger(1, "http-dedupe") == 1
    print("通过：10 轮各 10 并发重复回调，无锁升级死锁、无重复经济流水")
    assert f.request("POST", callback, order("http-dedupe"), signed=True, chunked=True)[0] == 200
    changed = order("http-dedupe")
    changed["amountMinor"] = 200
    assert f.request("POST", callback, changed, signed=True)[0] == 409
    print("通过：并发重复回调、分块实体验签、同键异载荷冲突与单次经济流水")

    assert f.request("GET", "/admin/tasks/demo/0", admin=viewer)[0] == 200
    assert f.request("POST", "/admin/announce/demo", {"bizKey": "deny", "serverId": 0, "content": "测试"}, admin=viewer)[0] == 403
    assert f.request("GET", "/admin/tasks/demo/0", admin=f.admin(games=["other"]))[0] == 403
    assert f.request("GET", "/admin/stats", admin=viewer)[0] == 403
    header = encode({"alg": "HS256"})
    for expiry in (None, int(time.time()), int(time.time()) - 1):
        claims = {"sub": "synthetic-viewer", "role": "viewer", "games": ["demo"], "iss": "gamehub-admin", "aud": "gamehub"}
        if expiry is not None:
            claims["exp"] = expiry
        message = header + b"." + encode(claims)
        import base64
        digest = base64.urlsafe_b64encode(hmac.new(f.env["GAMEHUB_ADMIN_JWT_SECRET"].encode(), message, hashlib.sha256).digest()).rstrip(b"=")
        assert f.request("GET", "/admin/tasks/demo/0", admin=(message + b"." + digest).decode())[0] == 401
    print("通过：管理角色、游戏范围、无有效期及过期 JWT 拒绝")

    f.receivers[1].fail = True
    status, body = f.request("POST", "/admin/announce/demo", {"bizKey": "broadcast", "serverId": 0, "content": "合成公告"}, admin=operator)
    assert status == 200
    broadcast = body["eventId"]
    wait_for(lambda: f.tasks(broadcast) == [2, 3], "广播部分失败", 15)
    task_id = f.sql(f"SELECT id FROM gamehub.delivery_task WHERE event_id={int(broadcast)} AND server_id=2")
    f.receivers[1].fail = False
    assert f.request("POST", f"/admin/tasks/demo/{task_id}/replay", admin=operator)[0] == 200
    wait_for(lambda: f.tasks(broadcast) == [2, 2], "广播失败目标重排")
    status, audit = f.request("GET", "/admin/audit/demo/0", admin=viewer)
    assert status == 200 and any(item["action"] == "replay_task" and item["targetId"] == task_id for item in audit["items"])
    print("通过：广播逐服状态、重试耗尽及审计重排")

    f.sql("USE gamehub; CREATE TRIGGER synthetic_reject BEFORE INSERT ON delivery_task FOR EACH ROW "
          "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic fault';")
    assert f.request("POST", callback, order("rollback-http"), signed=True)[0] == 503
    assert f.sql("SELECT COUNT(*) FROM gamehub.hub_event WHERE biz_key='rollback-http'") == "0"
    f.sql("DROP TRIGGER gamehub.synthetic_reject")
    status, body = f.request("POST", callback, order("rollback-http"), signed=True)
    assert status == 200 and not body["duplicate"]
    wait_for(lambda: f.tasks(body["eventId"]) == [2], "事务回滚后重试")
    print("通过：任务插入故障使订单一起回滚，恢复后同键成功受理")

    f.sql("USE gamehub; CREATE TRIGGER synthetic_delay BEFORE INSERT ON delivery_task FOR EACH ROW DO SLEEP(4)")
    started = time.monotonic()
    assert f.request("POST", callback, order("callback-deadline"), signed=True)[0] == 503
    assert time.monotonic() - started < 3.8
    f.sql("DROP TRIGGER gamehub.synthetic_delay")
    assert f.sql("SELECT COUNT(*) FROM gamehub.hub_event WHERE biz_key='callback-deadline'") == "0"
    # 故障期间后台检查可能暂时标记未就绪；先确认服务恢复，再验证同键受理和连接复用。
    wait_for(lambda: f.request("GET", "/admin/ready")[0] == 200, "慢 SQL 故障后恢复就绪")
    status, body = f.request("POST", callback, order("callback-deadline"), signed=True)
    assert status == 200 and not body["duplicate"]
    wait_for(lambda: f.tasks(body["eventId"]) == [2], "请求取消后连接池恢复")
    print("通过：慢 SQL 触发回调总时限、事务取消回滚及连接恢复")

    for version in (1, 2):
        assert f.request("POST", f"/admin/config/demo/{version}",
            {"bizKey": f"config:{version}", "serverId": 0, "content": {"syntheticVersion": version}}, admin=operator)[0] == 200
    status, latest = f.request("GET", "/internal/config/demo/1/0", signed=True)
    assert status == 200 and latest["version"] == 2
    assert f.request("POST", "/admin/config/demo/1", {"bizKey": "config:1", "serverId": 0,
        "content": {"syntheticVersion": 1}}, admin=operator)[1]["duplicate"]
    assert f.request("POST", "/admin/config/demo/1", {"bizKey": "config:1", "serverId": 0,
        "content": {"syntheticVersion": 999}}, admin=operator)[0] == 409
    assert f.request("GET", "/internal/config/demo/1/2")[0] == 401
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        published = list(pool.map(lambda _: f.request("POST", "/admin/config/demo/3", {
            "bizKey": "config:3", "serverId": 0, "content": {"syntheticVersion": 3}}, admin=operator), range(5)))
    assert all(status == 200 for status, _ in published)
    assert len({body["eventId"] for _, body in published}) == 1
    print("通过：不可变配置版本、单调发布、旧版本幂等及签名拉取")

    f.receivers[0].drop_once = True
    status, body = f.request("POST", callback, order("response-lost"), signed=True)
    assert status == 200
    wait_for(lambda: f.tasks(body["eventId"]) == [2], "提交后响应丢失重投")
    assert f.ledger(1, "response-lost") == 1
    f.receivers[0].bad_ack = True
    status, body = f.request("POST", callback, order("bad-ack"), signed=True)
    assert status == 200
    wait_for(lambda: f.tasks(body["eventId"]) == [3], "错误业务确认拒绝", 15)
    assert f.ledger(1, "bad-ack") == 1
    f.receivers[0].bad_ack = False
    print("通过：响应丢失不重复记账，HTTP 200 错误确认不误判成功")

    pause = threading.Event()
    f.receivers[0].pause = pause
    status, body = f.request("POST", callback, order("crash-recover"), signed=True)
    assert status == 200
    wait_for(lambda: f.tasks(body["eventId"]) == [1] and f.ledger(1, "crash-recover") == 1, "领取并提交后中断")
    f.hub.kill()
    f.hub.wait(timeout=5)
    pause.set()
    f.receivers[0].pause = None
    f.sql(f"UPDATE gamehub.delivery_task SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE event_id={int(body['eventId'])}")
    f.start_hub()
    wait_for(lambda: f.tasks(body["eventId"]) == [2], "进程中断后的租约恢复")
    assert f.ledger(1, "crash-recover") == 1
    print("通过：投递中进程强制中断、重启回收及游戏服持久化去重")

    verify_path_boundaries(f)
    f.callback_burst(callback)
    assert f.request("GET", "/admin/health")[0] == 200
    assert f.request("GET", "/admin/stats", admin=operator)[0] == 200
    assert f.request("GET", "/internal/config/demo/1/0", signed=True)[0] == 200
    print("通过：回调限流洪峰不挤占健康探针、管理接口和配置拉取")
    verify_retry_limit(f)
    from operations_api_integration import verify as verify_operations_api
    verify_operations_api(f)


def submitted(fixture, game_id, channel, key, server_id=1):
    """提交合成订单，返回受理事件号；不包含真实账号或玩家资料。"""
    body = order(key, server_id)
    status, result = fixture.request("POST", f"/api/callback/{game_id}/{channel}", body, signed=True)
    assert status == 200, f"合成通知受理失败：{game_id}，状态 {status}"
    return result["eventId"]


def verify_fair_delivery(fixture):
    """慢区服前方有积压时，同游戏其他区服及其他游戏仍能完成。"""
    pause = threading.Event()
    receiver = fixture.receivers[0]
    receiver.pause = pause
    try:
        slow = submitted(fixture, "demo", "demo", "multi-slow-0")
        wait_for(lambda: receiver.requests > 0, "慢目标已占用一个投递名额")
        for number in range(1, 5):
            submitted(fixture, "demo", "demo", f"multi-slow-{number}")
        same_game = submitted(fixture, "demo", "demo", "multi-fast-server", 2)
        other_game = submitted(fixture, "other", "merchant_a", "multi-fast-game")
        sample_beta = submitted(fixture, "sample_beta", "demo", "multi-sample-beta")
        wait_for(lambda: all(fixture.tasks(event_id) == [2] for event_id in (same_game, other_game, sample_beta)),
                 "慢目标期间其他目标与游戏完成", 2.5)
        assert fixture.tasks(slow) == [1], "验证隔离时慢目标已经释放，测试没有覆盖并发路径"
        status, runtime = fixture.request("GET", "/admin/runtime/demo", admin=fixture.admin(games=["demo"]))
        assert status == 200 and runtime["inFlight"] == 1
        assert runtime["maxConcurrentPerServer"] == 1
        assert any(target["serverId"] == 1 and target["inFlight"] == 1 for target in runtime["targets"])
    finally:
        pause.set()
        receiver.pause = None
    wait_for(lambda: fixture.tasks(slow) == [2], "慢目标恢复后完成")
    wait_for(lambda: fixture.request("GET", "/admin/stats/demo", admin=fixture.admin())[1]["pending"] == 0,
             "慢目标积压全部清空")
    print("通过：三款合成游戏轮转、慢区服并发隔离及通知投递")


def verify_scopes(fixture):
    """相同订单在独立商户下分别处理，同商户重复仍沿用原事件。"""
    first = submitted(fixture, "other", "merchant_a", "multi-merchant")
    second = submitted(fixture, "other", "merchant_b", "multi-merchant")
    assert first != second
    assert submitted(fixture, "other", "merchant_a", "multi-merchant") == first
    wait_for(lambda: fixture.tasks(first) == [2] and fixture.tasks(second) == [2], "商户隔离订单完成")
    with closing(sqlite3.connect(fixture.receivers[2].database)) as db:
        assert db.execute("SELECT COUNT(*) FROM synthetic_ledger WHERE biz_key='multi-merchant'").fetchone()[0] == 2
    viewer = fixture.admin(role="viewer", games=["other"])
    status, counts = fixture.request("GET", "/admin/stats/other", admin=viewer)
    assert status == 200 and counts["done"] >= 3
    status, runtime = fixture.request("GET", "/admin/runtime/other", admin=viewer)
    assert status == 200 and runtime["gameId"] == "other" and "games" not in runtime
    for endpoint in ("/admin/stats", "/admin/stats/demo", "/admin/runtime", "/admin/runtime/demo"):
        assert fixture.request("GET", endpoint, admin=viewer)[0] == 403
    print("通过：多商户业务身份、持久防重和按游戏统计授权")


def verify_worker_scope(fixture):
    """修改进程领取范围不会消耗其他游戏任务的尝试次数；暂停同样生效。"""
    fixture.configure_worker(["other"])
    pending = submitted(fixture, "demo", "demo", "multi-scope-pending")
    healthy = submitted(fixture, "other", "merchant_a", "multi-scope-other")
    wait_for(lambda: fixture.tasks(healthy) == [2], "指定游戏范围继续投递")
    assert fixture.sql(f"SELECT status,attempts FROM gamehub.delivery_task WHERE event_id={int(pending)}") == "0\t0"
    fixture.configure_worker(["demo"], enabled=False)
    snapshot = fixture.request("GET", "/admin/runtime/demo", admin=fixture.admin())[1]
    assert not snapshot["enabled"]
    assert fixture.sql(f"SELECT status,attempts FROM gamehub.delivery_task WHERE event_id={int(pending)}") == "0\t0"
    fixture.configure_worker([])
    wait_for(lambda: fixture.tasks(pending) == [2], "恢复游戏范围后原任务完成")
    print("通过：进程领取范围、暂停与恢复不会误领其他游戏")


def verify_callback_quota(fixture):
    """一个游戏耗尽回调桶后，其他游戏的合法回调与管理接口仍可用。"""
    fixture.callback_burst("/api/callback/other/merchant_a")
    submitted(fixture, "demo", "demo", "multi-independent-quota")
    assert fixture.request("GET", "/admin/runtime", admin=fixture.admin())[0] == 200
    assert fixture.request("GET", "/admin/ready")[0] == 200
    print("通过：按游戏入口配额不挤占其他游戏和管理接口")


def verify_multi_game(fixture):
    verify_fair_delivery(fixture)
    verify_scopes(fixture)
    verify_worker_scope(fixture)
    verify_callback_quota(fixture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--mysql-bin", type=Path, required=True)
    parser.add_argument("--suite", choices=("core", "multi-game", "all"), default="all",
                        help="选择基础故障、多游戏或全部用例，默认全部；每组使用独立临时实例")
    parser.add_argument("--timeout-seconds", type=float, default=600,
                        help="整轮测试总时限，默认 600 秒；到期后先取证再清理")
    parser.add_argument("--inject-failure", action="store_true",
                        help="服务就绪后人为报错，用于检查失败诊断和进程清理")
    args = parser.parse_args()
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("--timeout-seconds 必须是正数")
    run_suites(args.binary, args.mysql_bin, args.suite, args.timeout_seconds, args.inject_failure)


def run_suites(binary, mysql_bin, suite="all", timeout_seconds=600, inject_failure=False):
    """按 suite 创建独立实例，共用 timeout_seconds 总预算；失败立即取证退出，无返回值。"""
    suites = {"core": (Fixture, verify), "multi-game": (MultiGameFixture, verify_multi_game)}
    selected = suites.values() if suite == "all" else (suites[suite],)
    deadline = time.monotonic() + timeout_seconds
    for fixture_type, verifier in selected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IntegrationDeadlineExceeded("集成测试超过总时限，后续用例未启动")
        fixture = fixture_type(binary, mysql_bin)
        run_fixture(fixture, remaining, inject_failure, verifier=verifier)
    print("全部集成测试通过")


def run_fixture(fixture, timeout_seconds=600, inject_failure=False, *, verifier=None):
    """运行 fixture 后清理；verifier 默认使用基础验证，失败保留诊断并原样抛出，无返回值。"""
    try:
        with RunDeadline(timeout_seconds) as deadline:
            fixture.run_deadline = deadline
            fixture.start()
            deadline.timeout(timeout_seconds)
            if inject_failure:
                raise AssertionError("人为触发失败，用于检查诊断和进程清理")
            (verify if verifier is None else verifier)(fixture)
            deadline.timeout(timeout_seconds)
    except BaseException as failure:
        fixture.run_deadline = None
        try:
            fixture.close(failure=failure)
        except Exception:
            print("测试清理出错，原测试异常如下", file=sys.stderr)
        raise
    else:
        fixture.run_deadline = None
        fixture.close()
        print("本组集成测试通过，临时数据已清理")


if __name__ == "__main__":
    main()

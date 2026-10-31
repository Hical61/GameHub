"""用临时 MySQL 和模拟游戏服测容量、积压及故障恢复，不连接已有数据库。"""

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
import ctypes
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import integration


SCENARIOS = ("baseline", "multi-hub", "slow-target", "target-outage", "database-outage", "hub-crash")
STATUSES = ("pending", "delivering", "done", "failed")
DATABASE_ERROR_CATEGORIES = {"mysql.common-server", "mysql.mysql-server", "mysql.client", "system", "generic",
                             "asio.misc", "asio.netdb", "asio.addrinfo", "asio.ssl", "asio.ssl.stream"}
DELIVERY_ERROR_CATEGORIES = {"network_error", "response_too_large", "invalid_ack", "delivery_exception"}
DEADLOCK_COUNTER_FIXED_VERSION = (8, 0, 18)
DEADLOCK_COUNTER_BUG = "https://bugs.mysql.com/bug.php?id=77399"


def distribution(values):
    """values 为毫秒样本；返回数量和分位数，空样本不伪造为零延迟。"""
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    def percentile(fraction):
        return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)
    return {"count": len(ordered), "p50": percentile(0.50), "p95": percentile(0.95),
            "p99": percentile(0.99), "max": round(ordered[-1], 3)}


def process_sample(pid):
    """pid 是本次启动的 Hub；返回 RSS、句柄或 fd 数和累计 CPU，不能读取时返回 None。"""
    if os.name == "nt":
        from ctypes import wintypes
        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        query_information, vm_read = 0x0400, 0x0010
        handle = kernel.OpenProcess(query_information | vm_read, False, pid)
        if not handle:
            return None
        try:
            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            handles = wintypes.DWORD()
            kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetProcessHandleCount(handle, ctypes.byref(handles))
            times = [wintypes.FILETIME() for _ in range(4)]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
            cpu_ticks = sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in times[2:])
            return {"rssBytes": counters.WorkingSetSize, "handles": handles.value,
                    "cpuSeconds": round(cpu_ticks / 10_000_000, 6)}
        finally:
            kernel.CloseHandle(handle)
    try:
        status = (Path("/proc") / str(pid) / "status").read_text()
        rss = next(int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmRSS:"))
        stat = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return {"rssBytes": rss, "handles": len(list((Path("/proc") / str(pid) / "fd").iterdir())),
                "cpuSeconds": round((int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK"), 6)}
    except (OSError, StopIteration, ValueError):
        return None


def status_snapshot(fixture, prefix):
    """只统计本次合成业务前缀的任务；SQL 仍走 Fixture 的同会话 datadir 检查。"""
    rows = fixture.sql("SELECT t.status,COUNT(*),COALESCE(MAX(TIMESTAMPDIFF(MICROSECOND,t.created_at,"
                       "UTC_TIMESTAMP(6))),0) FROM gamehub.delivery_task t JOIN gamehub.hub_event e "
                       "ON e.id=t.event_id WHERE e.biz_key LIKE '" + prefix + "%' GROUP BY t.status")
    value = dict.fromkeys(STATUSES, 0)
    value["oldestPendingMs"] = 0
    for line in rows.splitlines():
        status, count, age = (int(item) for item in line.split("\t"))
        value[STATUSES[status]] = count
        if status == 0:
            value["oldestPendingMs"] = age / 1000
    return value


def counter_reliability(version, source):
    """按 MySQL 版本和计数来源标记已知漏计；未识别的版本不假定已经修复。"""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]|$)", version)
    if not match or "mariadb" in version.lower():
        return "unverified_version"
    if tuple(int(part) for part in match.groups()) < DEADLOCK_COUNTER_FIXED_VERSION:
        return "known_undercount" if source == "innodb_metrics.lock_deadlocks" else "unverified_legacy_counter"
    return "no_known_undercount"


def database_snapshot(fixture, enable_metric=False):
    """只读专用实例的累计死锁数和运行时间；UUID 仅用于内存中识别实例，不写入报告。"""
    lines = fixture.sql("SELECT @@server_uuid,VERSION(); SHOW GLOBAL STATUS WHERE Variable_name IN ('Innodb_deadlocks','Uptime')").splitlines()
    if not lines or len(identity := lines[0].split("\t")) != 2:
        raise RuntimeError("数据库观察字段不完整")
    fields = dict(line.split("\t") for line in lines[1:])
    source, enabled_during_read = "global_status.Innodb_deadlocks", False
    if "Innodb_deadlocks" in fields:
        deadlocks = int(fields["Innodb_deadlocks"])
    else:
        # performance_schema 关闭时，部分 MySQL 版本不再通过 SHOW 暴露 InnoDB 状态变量。
        query = "SELECT `COUNT`,STATUS FROM information_schema.INNODB_METRICS WHERE NAME='lock_deadlocks'"
        metric = fixture.sql(query).split("\t")
        if len(metric) == 2 and metric[1] != "enabled" and enable_metric:
            fixture.sql("SET GLOBAL innodb_monitor_enable='lock_deadlocks'")
            metric = fixture.sql(query).split("\t")
            enabled_during_read = True
        if len(metric) != 2 or metric[1] != "enabled":
            raise RuntimeError("死锁计数不可用或未启用，不能将缺失值当作零")
        deadlocks, source = int(metric[0]), "innodb_metrics.lock_deadlocks"
    if deadlocks < 0:
        raise RuntimeError("死锁计数无效")
    return {"serverUuid": identity[0], "mysqlVersion": identity[1], "deadlocks": deadlocks, "counterSource": source,
            "counterReliability": counter_reliability(identity[1], source),
            "counterEnabledDuringRead": enabled_during_read,
            "uptimeSeconds": int(fields["Uptime"]) if "Uptime" in fields else None, "observedMonotonic": time.monotonic(),
            "generation": fixture.__dict__.get("database_generation", 0)}


def deadlock_observation(start, end):
    """计算同一运行期的死锁增量；重启或计数复位时返回 reset，不能误报为零错误。"""
    def public(snapshot):
        return {key: snapshot[key] for key in ("deadlocks", "uptimeSeconds", "generation")} | {
            "counterSource": snapshot.get("counterSource", "global_status.Innodb_deadlocks"),
            "mysqlVersion": snapshot.get("mysqlVersion"),
            "counterReliability": snapshot.get("counterReliability", "unverified_version")}
    reset_reasons = []
    if start["generation"] != end["generation"]:
        reset_reasons.append("explicit_database_restart")
    if start["serverUuid"] != end["serverUuid"]:
        reset_reasons.append("server_changed")
    elapsed = end["observedMonotonic"] - start["observedMonotonic"]
    if (start["uptimeSeconds"] is not None and end["uptimeSeconds"] is not None
            and end["uptimeSeconds"] + 2 < start["uptimeSeconds"] + elapsed):
        reset_reasons.append("uptime_restarted")
    if start.get("counterSource") != end.get("counterSource"):
        reset_reasons.append("counter_source_changed")
    if end["deadlocks"] < start["deadlocks"]:
        reset_reasons.append("counter_decreased")
    reliable = all(snapshot.get("counterReliability") == "no_known_undercount" for snapshot in (start, end))
    complete = reliable and not end.get("counterEnabledDuringRead", False)
    result = {"complete": complete, "start": public(start), "end": public(end), "resetDetected": bool(reset_reasons),
              "resetReasons": reset_reasons, "delta": None if reset_reasons or not complete else end["deadlocks"] - start["deadlocks"]}
    if not complete:
        result["reason"] = "counter_was_disabled_at_end" if end.get("counterEnabledDuringRead") else "counter_reliability_incomplete"
    if any(snapshot.get("counterReliability") == "known_undercount" for snapshot in (start, end)):
        result.update(counterKnownUndercount=True, counterReliabilityReference=DEADLOCK_COUNTER_BUG)
    return result


def observe_database_restart(fixture):
    """重启就绪后恢复本次临时实例的计数开关；读取失败只记类型，不打断故障负载。"""
    result = {"generation": fixture.__dict__.get("database_generation", 0)}
    try:
        snapshot = database_snapshot(fixture, enable_metric=True)
        result.update(complete=snapshot["counterReliability"] == "no_known_undercount", deadlocks=snapshot["deadlocks"],
                      counterSource=snapshot["counterSource"], mysqlVersion=snapshot["mysqlVersion"],
                      counterReliability=snapshot["counterReliability"])
    except Exception as error:
        result.update(complete=False, errorType=type(error).__name__)
    fixture.__dict__.setdefault("capacity_restart_observations", []).append(result)


def log_error_counts(fixture, offsets):
    """只统计本次负载窗口已写出的固定日志类别和数字，不保留原行、SQL、任务号或载荷。"""
    database, delivery = Counter(), Counter()
    fixed = Counter()
    scanned = 0
    pattern = re.compile(r"category=([A-Za-z0-9_.-]{1,64}) code=(-?[0-9]{1,10})(?![A-Za-z0-9_])")
    for log in fixture.logs:
        if not log.closed:
            log.flush()
    for path in fixture.path.glob("hub*.log"):
        with path.open("rb") as stream:
            stream.seek(min(offsets.get(path.name, 0), path.stat().st_size))
            while block := stream.readline(8192):
                scanned += len(block)
                line = block.decode("utf-8", errors="replace")
                kind = ("deadlockExhausted" if "数据库死锁重试耗尽" in line else
                        "deadlockRetry" if "数据库死锁重试" in line else
                        "deliveryStorage" if "投递时数据库操作失败" in line else
                        "requestDependency" if "处理请求时依赖服务出错" in line else None)
                if kind:
                    match = pattern.search(line)
                    if match and match[1] in DATABASE_ERROR_CATEGORIES and -(2 ** 31) <= int(match[2]) < 2 ** 31:
                        database[(kind, match[1], int(match[2]))] += 1
                    else:
                        fixed[kind + "Unclassified"] += 1
                if "投递失败，taskId=" in line:
                    match = re.search(r"category=([A-Za-z0-9_]{1,48})(?![A-Za-z0-9_])", line)
                    if match and (match[1] in DELIVERY_ERROR_CATEGORIES or re.fullmatch(r"http_[1-5][0-9]{2}", match[1])):
                        delivery[match[1]] += 1
                for marker, name in (("数据库或迁移检查失败", "schemaCheckFailures"),
                                     ("数据库连接池后台循环异常退出", "databaseLoopFailures"),
                                     ("收到投递结果时租约已失效", "staleLeaseResults")):
                    if marker in line:
                        fixed[name] += 1
                if kind == "deadlockRetry":
                    fixed["deadlockRetries"] += 1
                elif kind == "deadlockExhausted":
                    fixed["deadlockExhausted"] += 1
                recovered = re.search(r"已回收过期的投递租约，count=([0-9]{1,10})(?![0-9])", line)
                if recovered:
                    fixed["recoveredLeaseTasks"] += int(recovered[1])
    return {"complete": True, "databaseErrors": [{"kind": kind, "category": category, "code": code, "count": count}
            for (kind, category, code), count in sorted(database.items())],
            "deliveryFailures": dict(sorted(delivery.items())), "fixedCounts": dict(sorted(fixed.items())),
            "scannedBytes": scanned,
            "scope": "只计已写出的本次负载日志；强杀进程前尚未写出的日志可能丢失。"}


def begin_database_observation(fixture):
    """保存本次负载起点，不输出或复制日志内容。"""
    try:
        fixture.capacity_database_start = database_snapshot(fixture, enable_metric=True)
    except Exception as error:
        fixture.capacity_database_start = None
        fixture.capacity_database_start_error = type(error).__name__
    fixture.capacity_log_offsets = {path.name: path.stat().st_size for path in fixture.path.glob("hub*.log")}


def begin_measurement(fixture, worker, report):
    """记录固定的非敏感运行参数和错误计数起点，不把整份配置或环境放入报告。"""
    settings = fixture.sql("SELECT @@innodb_buffer_pool_size,@@max_connections,@@innodb_flush_log_at_trx_commit")
    report.update(worker=worker, mysqlVersion=fixture.sql("SELECT VERSION()"), processCount=len(fixture.hubs),
                  databaseSettings=dict(zip(("innodbBufferPoolBytes", "maxConnections", "flushLogAtTrxCommit"),
                                             (int(value) for value in settings.split("\t")))))
    begin_database_observation(fixture)


def finish_database_observation(fixture, report):
    """清理前留下死锁变化和固定日志计数；读取失败标记不完整，不声称错误数为零。"""
    observation = {}
    try:
        start = fixture.__dict__.get("capacity_database_start")
        if start is None:
            error_type = fixture.__dict__.get("capacity_database_start_error")
            observation["deadlocks"] = {"complete": False,
                "reason": "counter_unavailable_at_start" if error_type else "load_did_not_start"}
            if error_type:
                observation["deadlocks"]["errorType"] = error_type
        else:
            observation["deadlocks"] = deadlock_observation(start, database_snapshot(fixture, enable_metric=True))
    except Exception as error:
        observation["deadlocks"] = {"complete": False, "errorType": type(error).__name__}
    if start is not None and fixture.__dict__.get("database_generation", 0) != start.get("generation", 0):
        deadlocks = observation["deadlocks"]
        deadlocks.update(resetDetected=True, delta=None)
        reasons = deadlocks.setdefault("resetReasons", [])
        if "explicit_database_restart" not in reasons:
            reasons.append("explicit_database_restart")
    if restarts := fixture.__dict__.get("capacity_restart_observations"):
        observation["restartCounters"] = restarts
    try:
        observation["writtenHubLogs"] = log_error_counts(fixture, fixture.__dict__.get("capacity_log_offsets", {}))
    except Exception as error:
        observation["writtenHubLogs"] = {"complete": False, "errorType": type(error).__name__}
    report["databaseDiagnostics"] = observation


def track_connections(receiver):
    """记录接收端活跃连接，模拟端口失联时连已建立的保活连接一起断开。"""
    receiver.capacity_connections = set()
    receiver.capacity_connection_lock = threading.Lock()
    original_accept, original_close = receiver.get_request, receiver.close_request
    def accept():
        connection, address = original_accept()
        with receiver.capacity_connection_lock:
            receiver.capacity_connections.add(connection)
        return connection, address
    def close(connection):
        try:
            original_close(connection)
        finally:
            with receiver.capacity_connection_lock:
                receiver.capacity_connections.discard(connection)
    receiver.get_request, receiver.close_request = accept, close


class CapacityFixture(integration.Fixture):
    """沿用隔离数据库和失败取证；另外记录重启临时数据库所需的原始命令。"""

    def launch(self, command, name, **kwargs):
        """启动 command 并按 name 记日志；返回已登记的子进程，重启时复用临时数据库参数。"""
        if name == "mysql":
            self.mysql_command = list(command)
        return super().launch(command, name, **kwargs)

    def configure_hubs(self, options):
        """options 指定并发和轮询参数；重启演示 Hub 后返回实际使用的非敏感 Worker 配置。"""
        self._stop_child(self.hub)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["worker"].update(pollMs=options.poll_ms, concurrency=options.slots, maxAttempts=8,
                                baseRetrySeconds=1, maxRetrySeconds=2, leaseSeconds=30,
                                connectTimeoutMs=500, requestTimeoutMs=1000)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.start_hub()
        self.hubs = [self.hub]
        if options.scenario == "multi-hub":
            config["server"]["port"] = integration.free_port()
            second = self.path / "second-hub.json"
            second.write_text(json.dumps(config), encoding="utf-8")
            child = self.launch([str(self.binary), "--config", str(second)], "hub-secondary")
            self.hubs.append(child)
            integration.wait_for(lambda: child.poll() is None and self._port_ready(config["server"]["port"]),
                                 "第二个 Hub 就绪")
        return config["worker"]

    def _port_ready(self, port):
        import http.client
        try:
            with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=1)) as connection:
                connection.request("GET", "/admin/ready")
                response = connection.getresponse()
                response.read()
                return response.status == 200
        except OSError:
            return False

    def restart_database(self):
        """只重启本次 Fixture 的数据目录，等待同会话身份检查重新通过。"""
        self.db = self.launch(self.mysql_command, "mysql")
        self.database_generation = self.__dict__.get("database_generation", 0) + 1
        def ready():
            if self.db.poll() is not None:
                raise RuntimeError("临时数据库重启失败")
            try:
                return self.sql("SELECT 1") == "1"
            except (RuntimeError, subprocess.TimeoutExpired):
                return False
        integration.wait_for(ready, "临时数据库重启", 45)
        observe_database_restart(self)


class Sampler:
    """每秒采样本次 Hub 和测试库，不把 SQL 原文或环境写入结果。"""

    def __init__(self, fixture, prefix):
        """fixture 为本次专用环境，prefix 为合成业务前缀；采样线程由调用者启动。"""
        self.fixture, self.prefix = fixture, prefix
        self.samples = []
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        """采样直到收到停止信号，无返回值；数据库不可用时保留该时间点的故障标记。"""
        while not self.stop.is_set():
            value = {"seconds": round(time.monotonic() - self.started, 3), "hubs": []}
            for index, process in enumerate(list(self.fixture.hubs)):
                value["hubs"].append({"slot": index, "running": process.poll() is None,
                                      "resource": process_sample(process.pid) if process.poll() is None else None})
            try:
                value["tasks"] = status_snapshot(self.fixture, self.prefix)
                value["databaseConnections"] = int(self.fixture.sql(
                    "SELECT COUNT(*) FROM information_schema.PROCESSLIST"))
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                value["databaseUnavailable"] = True
            self.samples.append(value)
            self.stop.wait(1)

    def close(self):
        """停止并等待采样线程；未能退出时抛错，不静默丢掉后台工作。"""
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=25)
        if self.thread.is_alive():
            raise RuntimeError("采样线程未退出")


def submit_one(fixture, prefix, index, broadcast_every):
    """发送一笔合成业务；503/429 用原业务键重试，返回状态和耗时，不返回载荷。"""
    began = time.monotonic()
    key = prefix + str(index)
    broadcast = broadcast_every and index % broadcast_every == 0
    status, value, attempts = 0, {}, 0
    for attempts in range(1, 6):
        try:
            if broadcast:
                status, value = fixture.request("POST", "/admin/announce/demo",
                    {"bizKey": key, "serverId": 0, "content": "合成容量测试"}, admin=fixture.admin())
            else:
                status, value = fixture.request("POST", "/api/callback/demo/demo", integration.order(key), signed=True)
        except (OSError, TimeoutError, http.client.HTTPException):
            status, value = 0, {}
        if status not in (0, 200, 429, 503):
            raise AssertionError(f"合成请求返回非预期状态码 {status}，没有形成有效压测")
        if status not in (0, 429, 503):
            break
        time.sleep(min(1, 0.1 * attempts))
    return {"index": index, "status": status, "eventId": value.get("eventId"), "attempts": attempts,
            "requestMs": (time.monotonic() - began) * 1000, "broadcast": bool(broadcast)}


def run_load(fixture, options, prefix):
    """在指定时间内按速率发请求；客户端满载时记录少发数量，不把压测无限拖长。"""
    count = max(1, math.ceil(options.duration_seconds * options.rate))
    started = time.monotonic()
    ends = started + options.duration_seconds
    results, active = [], set()
    with ThreadPoolExecutor(max_workers=options.clients) as pool:
        for index in range(count):
            if time.monotonic() >= ends:
                break
            deadline = started + index / options.rate
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            while len(active) >= options.clients:
                completed = wait(active, timeout=max(0, ends - time.monotonic()), return_when=FIRST_COMPLETED).done
                if not completed:
                    break
                active.difference_update(completed)
                results.extend(item.result() for item in completed)
            if time.monotonic() >= ends:
                break
            active.add(pool.submit(submit_one, fixture, prefix, index, options.broadcast_every))
        results.extend(item.result() for item in active)
        remaining = ends - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
    return results, time.monotonic() - started


def apply_fault(fixture, options, prefix, report, cancel, failures=None):
    """只操作本次进程或接收端；记录故障窗口，恢复失败会通过主线程报错。"""
    if cancel.wait(min(2, options.duration_seconds / 3)):
        return
    began = time.monotonic()
    try:
        if options.scenario == "database-outage":
            fixture.sql("SHUTDOWN")
            fixture.db.wait(timeout=15)
            if cancel.wait(options.fault_seconds):
                return
            fixture.restart_database()
        elif options.scenario == "target-outage":
            receiver = next(item for item in fixture.receivers if item.server_id == 1)
            address, database = receiver.server_address, receiver.database
            receiver.shutdown()
            if hasattr(receiver, "capacity_connections"):
                with receiver.capacity_connection_lock:
                    connections = list(receiver.capacity_connections)
                for connection in connections:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            receiver.server_close()
            fixture.receiver_threads[receiver].join(timeout=2)
            if cancel.wait(options.fault_seconds):
                return
            replacement = integration.DemoReceiver(address, database,
                fixture.env["GAMEHUB_DEMO_OUTBOUND_SECRET"], server_id=1)
            fixture._start_receiver(replacement)
            fixture.receivers.remove(receiver)
        elif options.scenario == "hub-crash":
            integration.wait_for(lambda: status_snapshot(fixture, prefix)["delivering"] > 0,
                                 "任务进入投递后强制终止", 15)
            fixture.hub.kill()
            fixture.hub.wait(timeout=5)
            if cancel.wait(options.fault_seconds):
                return
            fixture.start_hub()
            fixture.hubs[0] = fixture.hub
        report["fault"] = {"kind": options.scenario, "elapsedSeconds": round(time.monotonic() - began, 3)}
    except BaseException as error:
        report["faultFailureType"] = type(error).__name__
        if failures is not None:
            failures.append(error)


def verify_result(fixture, prefix):
    """核对任务完成和模拟权威流水；SQL 与 SQLite 都只读本次合成记录。"""
    state = status_snapshot(fixture, prefix)
    if state["pending"] or state["delivering"] or state["failed"]:
        raise AssertionError("容量场景结束后仍有未完成任务")
    event_counts = fixture.sql("SELECT task_type,COUNT(*) FROM gamehub.hub_event "
                               "WHERE biz_key LIKE '" + prefix + "%' GROUP BY task_type")
    counts = {line.split("\t")[0]: int(line.split("\t")[1]) for line in event_counts.splitlines()}
    if not counts or not state["done"]:
        raise AssertionError("没有实际受理并完成的任务，不能将空负载判为通过")
    expected = counts.get("recharge_notify", 0)
    expected_tasks = expected + counts.get("announce", 0) * len(fixture.receivers)
    if state["done"] != expected_tasks:
        raise AssertionError("事件生成的区服任务数量不完整")
    ledger = 0
    for receiver in fixture.receivers:
        with closing(sqlite3.connect(receiver.database)) as database:
            count, distinct_count = database.execute(
                "SELECT COUNT(*),COUNT(DISTINCT biz_key) FROM synthetic_ledger WHERE biz_key LIKE ?", (prefix + "%",)).fetchone()
            if count != distinct_count:
                raise AssertionError("模拟经济流水出现重复")
            ledger += count
    if ledger != expected:
        raise AssertionError("Hub 事件与模拟经济流水数量不一致")
    latency = fixture.sql("SELECT TIMESTAMPDIFF(MICROSECOND,t.created_at,t.updated_at)/1000 "
        "FROM gamehub.delivery_task t JOIN gamehub.hub_event e ON e.id=t.event_id "
        "WHERE e.biz_key LIKE '" + prefix + "%' AND t.status=2")
    return state, ledger, distribution([float(value) for value in latency.splitlines()])


def assess_requests(results, prefix, persisted):
    """逐笔核对本次合成请求；未获 HTTP 确认与库中未观察到记录分别计数。"""
    confirmed, unconfirmed = [], []
    for row in results:
        if row["status"] not in (0, 200, 429, 503):
            raise AssertionError("负载包含认证或字段等非预期错误")
        observed_id = persisted.get(prefix + str(row["index"]))
        if row["status"] == 200:
            if not observed_id or row["eventId"] != observed_id:
                raise AssertionError("已确认受理的事件丢失或身份不匹配")
            confirmed.append(observed_id)
        else:
            unconfirmed.append({"index": row["index"], "status": row["status"],
                                "resolution": "persisted_without_http_ack" if observed_id else "not_observed_in_hub_at_end"})
    if len(set(confirmed)) != len(confirmed):
        raise AssertionError("不同业务返回了同一个事件身份")
    return {"validLoad": bool(confirmed), "httpConfirmedCount": len(confirmed),
            "allRequestsHttpConfirmed": bool(results) and not unconfirmed,
            "persistedWithoutHttpAckCount": sum(row["resolution"] == "persisted_without_http_ack" for row in unconfirmed),
            "notObservedInHubAtEndCount": sum(row["resolution"] == "not_observed_in_hub_at_end" for row in unconfirmed),
            "unconfirmed": unconfirmed}


def cleanup_measurement(sampler, fault, cancel, report, original_failure):
    """无论启动到哪一步都尝试停线程；清理错误单独记录，不替换已有测试失败。"""
    cancel.set()
    sampler.stop.set()
    errors = []
    actions = [sampler.close]
    if fault.ident is not None:
        actions.insert(0, lambda: fault.join(timeout=65))
    for action in actions:
        try:
            action()
        except Exception as error:
            errors.append(type(error).__name__)
    if fault.is_alive():
        errors.append("FaultThreadStillRunning")
    if errors:
        report["measurementCleanupErrors"] = errors
        if original_failure is None:
            raise RuntimeError("容量采样或故障线程清理失败")


def execute(fixture, options, report):
    """运行一个场景，写入去掉业务身份和凭据的测量结果。"""
    if options.scenario == "target-outage":
        # 先重启接收循环，确保第一次 accept 也使用连接跟踪函数。
        receiver = next(item for item in fixture.receivers if item.server_id == 1)
        receiver.shutdown()
        fixture.receiver_threads[receiver].join(timeout=2)
        track_connections(receiver)
        fixture.receiver_threads[receiver] = threading.Thread(target=receiver.serve_forever, daemon=True)
        fixture.receiver_threads[receiver].start()
    worker = fixture.configure_hubs(options)
    prefix = "capacity_" + uuid.uuid4().hex + "_"
    if options.scenario == "slow-target":
        receiver = fixture.receivers[0]
        original = receiver.commit
        def delayed(envelope):
            time.sleep(options.slow_seconds)
            return original(envelope)
        receiver.commit = delayed
    if options.scenario == "hub-crash":
        # 放慢确认，使“投递中强杀”可重复，而不是碰运气抓几毫秒的窗口。
        fixture.receivers[0].pause = threading.Event()
    begin_measurement(fixture, worker, report)
    sampler = Sampler(fixture, prefix)
    cancel = threading.Event()
    fault_failures = []
    fault = threading.Thread(target=apply_fault, args=(fixture, options, prefix, report, cancel, fault_failures), daemon=True)
    started = time.monotonic()
    failure = None
    try:
        sampler.thread.start()
        if options.scenario.endswith("outage") or options.scenario == "hub-crash":
            fault.start()
        results, load_seconds = run_load(fixture, options, prefix)
        if fault.ident:
            fault.join(timeout=65)
            if fault_failures:
                raise fault_failures[0]
            if fault.is_alive():
                raise AssertionError("故障控制或恢复未完成")
        if options.scenario == "hub-crash":
            fixture.receivers[0].pause.set()
            fixture.receivers[0].pause = None
        load_end = time.monotonic()
        integration.wait_for(lambda: (lambda state: state["pending"] == state["delivering"] == 0)(
            status_snapshot(fixture, prefix)), "积压全部处理完成", options.drain_timeout)
        persisted = fixture.sql("SELECT biz_key,id FROM gamehub.hub_event WHERE biz_key LIKE '" + prefix + "%'")
        assessment = assess_requests(results, prefix, dict(line.split("\t") for line in persisted.splitlines()))
        report["requestAssessment"] = assessment
        if not assessment["validLoad"]:
            raise AssertionError("没有成功获得 HTTP 受理确认的业务，本次未形成有效负载")
        state, ledger, completion = verify_result(fixture, prefix)
        elapsed = time.monotonic() - started
        report.update(requests=len(results), confirmedAccepted=sum(row["status"] == 200 for row in results),
            scheduledRequests=max(1, math.ceil(options.duration_seconds * options.rate)),
            missedScheduleRequests=max(1, math.ceil(options.duration_seconds * options.rate)) - len(results),
            unconfirmedRequests=sum(row["status"] != 200 for row in results),
            statusCounts={str(status): sum(row["status"] == status for row in results)
                          for status in sorted({row["status"] for row in results})},
            httpAttemptCount=sum(row["attempts"] for row in results), requestLatencyMs=distribution(
                [row["requestMs"] for row in results]), databaseCompletionLatencyMs=completion,
            loadSeconds=round(load_seconds, 3), elapsedSeconds=round(elapsed, 3),
            submissionWindowSeconds=options.duration_seconds,
            requestCompletionTailSeconds=round(max(0, load_seconds - options.duration_seconds), 3),
            offeredBusinessPerSecond=round(len(results) / options.duration_seconds, 3),
            drainSeconds=round(time.monotonic() - load_end, 3),
            acceptedPerSecond=round(sum(row["status"] == 200 for row in results) / load_seconds, 3),
            callbackAcceptedCount=sum(row["status"] == 200 and not row["broadcast"] for row in results),
            announcementAcceptedCount=sum(row["status"] == 200 and row["broadcast"] for row in results),
            generatedTasksPerSecond=round(sum(state[name] for name in STATUSES) / load_seconds, 3),
            confirmedTasksPerSecond=round(state["done"] / elapsed, 3), taskCounts=state,
            syntheticLedgerCount=ledger, samples=sampler.samples)
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup_measurement(sampler, fault, cancel, report, failure)


def main():
    """运行所选场景并写 JSON；返回 0 表示一致性检查通过，1 表示失败。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--mysql-bin", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="baseline")
    parser.add_argument("--duration-seconds", type=float, default=10)
    parser.add_argument("--rate", type=float, default=8, help="每秒业务请求数，广播按区服生成多条任务")
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--slots", type=int, default=4)
    parser.add_argument("--poll-ms", type=int, default=1000)
    parser.add_argument("--broadcast-every", type=int, default=5, help="每 N 笔发一次两服公告，0 表示只有订单")
    parser.add_argument("--slow-seconds", type=float, default=0.25)
    parser.add_argument("--fault-seconds", type=float, default=3)
    parser.add_argument("--drain-timeout", type=float, default=90)
    parser.add_argument("--output", type=Path, default=integration.ROOT / ".local/capacity-result.json")
    options = parser.parse_args()
    if not (1 <= options.duration_seconds <= 600 and 0 < options.rate <= 1000 and 1 <= options.clients <= 64
            and 1 <= options.slots <= 32 and 10 <= options.poll_ms <= 60000 and options.broadcast_every >= 0
            and 0 <= options.slow_seconds <= 5 and 0 < options.fault_seconds <= 10 and options.drain_timeout > 0):
        parser.error("容量参数超出允许范围")
    digest = hashlib.sha256()
    with options.binary.open("rb") as binary:
        for chunk in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(chunk)
    report = {"formatVersion": 1, "capturedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "scenario": options.scenario, "platform": platform.platform(), "pythonVersion": platform.python_version(),
              "cpuCount": os.cpu_count(), "binarySha256": digest.hexdigest(),
              "requestedRate": options.rate, "durationSeconds": options.duration_seconds,
              "clients": options.clients, "broadcastEvery": options.broadcast_every,
              "slowSeconds": options.slow_seconds if options.scenario == "slow-target" else None,
              "note": "结果仅对应本次合成负载和配置，不代表生产容量或长时间稳定性验收。"}
    fixture = CapacityFixture(options.binary, options.mysql_bin)
    failure = None
    try:
        fixture.start()
        execute(fixture, options, report)
        report["passed"] = True
    except BaseException as error:
        failure = error
        report.update(passed=False, failureType=type(error).__name__)
    finally:
        finish_database_observation(fixture, report)
        try:
            fixture.close(failure=failure)
        except Exception as error:
            report["cleanupFailureType"] = type(error).__name__
            report["passed"] = False
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    complete = report.get("requestAssessment", {}).get("allRequestsHttpConfirmed", False)
    outcome = "通过，全部请求获得 HTTP 确认" if complete else "通过，部分请求未获 HTTP 确认"
    print("容量场景一致性检查" + (outcome if report["passed"] else "失败") + "，结果：" + str(options.output.resolve()))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

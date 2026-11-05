"""在独立临时 MySQL 中执行数小时长稳验证，在线聚合并限制内存样本数量。"""

import argparse
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import integration
from capacity import process_sample, status_snapshot


class Summary:
    """固定直方图和有界资源样本，不随订单数量增长。"""

    def __init__(self, sample_limit=256):
        """sample_limit 限定保留的最近资源采样数量。"""
        self.statuses = Counter()
        self.latency_bins = [0] * 32
        self.count = 0
        self.max_latency_ms = 0
        self.samples = deque(maxlen=sample_limit)
        self.sample_count = 0
        self.peak_rss_bytes = 0
        self.peak_handles = 0
        self.first_resource = None
        self.last_resource = None

    def observe(self, status, milliseconds):
        """按 HTTP 状态和以二为底的毫秒直方图累加一笔请求。"""
        self.count += 1
        self.statuses[str(status)] += 1
        self.latency_bins[min(31, max(0, math.ceil(math.log2(max(1, milliseconds)))))] += 1
        self.max_latency_ms = max(self.max_latency_ms, milliseconds)

    def sample(self, value):
        """保留有界最近样本，峰值覆盖整轮而非仅最近窗口。"""
        self.samples.append(value)
        self.sample_count += 1
        resource = value.get("resource") or {}
        if resource:
            self.first_resource = self.first_resource or resource
            self.last_resource = resource
        self.peak_rss_bytes = max(self.peak_rss_bytes, resource.get("rssBytes", 0))
        self.peak_handles = max(self.peak_handles, resource.get("handles", 0))

    def report(self):
        """返回聚合结果；直方图是固定桶上界，不伪装成精确分位数。"""
        return {"requests": self.count, "httpStatusCounts": dict(self.statuses),
                "latencyHistogramMs": [{"upperBound": 2**index, "count": count}
                                       for index, count in enumerate(self.latency_bins)],
                "maxRequestMs": round(self.max_latency_ms, 3), "resourceSampleCount": self.sample_count,
                "peakRssBytes": self.peak_rss_bytes, "peakHandles": self.peak_handles,
                "firstResource": self.first_resource, "lastResource": self.last_resource,
                "recentSamples": list(self.samples), "sampleLimit": self.samples.maxlen}


def send(fixture, key):
    """只发送一笔合成回调，失败不换业务身份；返回状态和耗时，不保留载荷。"""
    started = time.monotonic()
    try:
        status, _ = fixture.request("POST", "/api/callback/demo/demo", integration.order(key), signed=True)
    except (OSError, TimeoutError):
        status = 0
    if status not in (0, 200, 429, 503):
        raise AssertionError("长稳负载被协议或鉴权拒绝")
    return status, (time.monotonic() - started) * 1000


def sample_tasks(fixture):
    """采样固定分片的游戏统计，不按随负载增长的事件表反复计数。"""
    status, value = fixture.request("GET", "/admin/stats/demo", admin=fixture.admin(role="viewer", games=["demo"]))
    names = ("pending", "delivering", "done", "failed", "archivedDone", "oldestPendingSeconds")
    if status != 200 or not isinstance(value, dict) or any(
            type(value.get(name)) is not int or value[name] < 0 for name in names):
        raise ValueError("长稳游戏统计不可用，不能将缺失观测当作零积压")
    return {name: value[name] for name in names}


def verify_ledger(fixture, prefix, expected_count):
    """分批核对真实合成账本身份和金额；唯一键加完整序号范围及数量证明没有同数量错单。"""
    expected = integration.order(prefix + "0")
    target = expected["serverId"]
    valid = sum(receiver.server_id == target for receiver in fixture.receivers) == 1
    count = 0
    for receiver in fixture.receivers:
        deadline = time.monotonic() + fixture.operation_timeout(3600)
        with closing(sqlite3.connect(receiver.database, timeout=fixture.operation_timeout(5))) as connection:
            connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            cursor = connection.execute("SELECT game_id,server_id,namespace_key,biz_key,amount_minor "
                                        "FROM synthetic_ledger WHERE biz_key LIKE ?", (prefix + "%",))
            while True:
                fixture.operation_timeout(5)
                rows = cursor.fetchmany(1024)
                if not rows:
                    break
                for game, server, namespace, key, amount in rows:
                    count += 1
                    suffix = key[len(prefix):]
                    sequence_ok = suffix.isascii() and suffix.isdecimal() and len(suffix) <= 20
                    if sequence_ok:
                        number = int(suffix)
                        sequence_ok = str(number) == suffix and 0 <= number < expected_count
                    valid = valid and (game == "demo" and server == target == receiver.server_id
                                       and namespace == "demo" and amount == expected["amountMinor"] and sequence_ok)
    return count, valid and count == expected_count


def run_load(fixture, options):
    """有界并发执行负载，再在单独排空预算内核对 Hub 与权威模拟账本。"""
    summary = Summary(options.sample_limit)
    prefix = "soak-" + uuid.uuid4().hex + "-"
    game_baseline = sample_tasks(fixture)
    began = time.monotonic()
    end = began + options.duration_seconds
    next_request = began
    next_sample = began
    sent = missed = 0
    active = set()
    pool = ThreadPoolExecutor(max_workers=options.clients)
    try:
        while time.monotonic() < end or active:
            fixture.operation_timeout(10)
            now = time.monotonic()
            if now < end and now >= next_request:
                due = min(math.floor((now - next_request) * options.rate) + 1,
                          math.ceil((end - next_request) * options.rate))
                available = max(0, options.clients - len(active))
                for _ in range(min(due, available)):
                    active.add(pool.submit(send, fixture, prefix + str(sent)))
                    sent += 1
                missed += max(0, due - available)
                next_request += due / options.rate
            if now >= next_sample:
                summary.sample({"seconds": round(now - began, 3), "resource": process_sample(fixture.hub.pid),
                                "tasks": sample_tasks(fixture)})
                next_sample = now + options.sample_interval
            if active:
                finished, active = wait(active, timeout=min(0.1, fixture.operation_timeout(0.1)),
                                        return_when=FIRST_COMPLETED)
                for future in finished:
                    summary.observe(*future.result())
            elif now < end:
                time.sleep(min(0.1, max(0, next_request - now), fixture.operation_timeout(0.1)))
    finally:
        # HTTP 使用 Fixture 的整轮截止点；先收回线程，之后才能停止和删除临时实例。
        pool.shutdown(wait=True, cancel_futures=True)
    drain_end = time.monotonic() + options.drain_seconds
    while True:
        state = status_snapshot(fixture, prefix)
        if state["pending"] + state["delivering"] == 0:
            break
        if time.monotonic() >= drain_end:
            raise TimeoutError("长稳排空预算已耗尽")
        time.sleep(min(0.2, fixture.operation_timeout(0.2)))
    persisted = int(fixture.sql("SELECT COUNT(*) FROM gamehub.hub_event WHERE biz_key LIKE '" + prefix + "%'") or "0")
    ledger_count, ledger_matches = verify_ledger(fixture, prefix, sent)
    report = summary.report()
    report.update(durationSeconds=options.duration_seconds, missedScheduleRequests=missed,
                  taskSampleScope="game", gameTaskBaseline=game_baseline, finalTaskCountScope="run-prefix",
                  persistedEvents=persisted, taskCounts=state, ledgerEntries=ledger_count, ledgerIdentityMatched=ledger_matches,
                  passed=(sent > 0 and summary.statuses["200"] == sent and missed == 0
                          and persisted == sent == state["done"] == ledger_count and state["failed"] == 0 and ledger_matches))
    return report


def run(options, fixture_factory=integration.Fixture):
    """只创建本次独立实例；成功和异常都复用已有取证与受控清理流程。"""
    fixture = fixture_factory(options.binary, options.mysql_bin)
    failure = None
    try:
        with integration.RunDeadline(options.setup_seconds + options.duration_seconds + options.drain_seconds) as deadline:
            fixture.run_deadline = deadline
            fixture.start()
            report = run_load(fixture, options)
            if not report.get("passed"):
                failure = AssertionError("长稳结果未满足完整受理、排空和账本一致性要求")
            return report
    except BaseException as error:
        failure = error
        raise
    finally:
        fixture.run_deadline = None
        try:
            fixture.close(failure)
        except BaseException:
            if failure is None:
                raise
            print("长稳清理异常，保留原始失败，请检查已保留的诊断目录。", file=sys.stderr)


def main():
    """运行隔离长稳测试；总时限、输出和输入均在创建实例前校验。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--mysql-bin", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=14400)
    parser.add_argument("--setup-seconds", type=float, default=180)
    parser.add_argument("--drain-seconds", type=float, default=120)
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--sample-interval", type=float, default=30)
    parser.add_argument("--sample-limit", type=int, default=256)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    numbers = (options.duration_seconds, options.setup_seconds, options.drain_seconds, options.rate, options.sample_interval)
    if (any(not math.isfinite(value) for value in numbers) or not 1 <= options.duration_seconds <= 7 * 86400
            or not 1 <= options.setup_seconds <= 3600 or not 1 <= options.drain_seconds <= 3600
            or not 0 < options.rate <= 1000 or not 1 <= options.clients <= 64
            or not 1 <= options.sample_interval <= 3600 or not 1 <= options.sample_limit <= 4096):
        parser.error("长稳参数超出范围")
    for source in (options.binary, options.mysql_bin):
        if options.output.resolve() == source.resolve() or (options.output.exists() and source.is_file()
                                                           and options.output.samefile(source)):
            parser.error("输出不能覆盖程序或输入目录")
    failure = None
    report = {"formatVersion": 1, "scope": "isolated-synthetic", "passed": False}
    try:
        report.update(run(options))
    except Exception as error:
        failure = error
        report["errorType"] = type(error).__name__
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("隔离长稳验证" + ("通过" if report["passed"] else "未通过") + "，报告：" + str(options.output.resolve()))
    return 0 if report["passed"] and failure is None else 1


if __name__ == "__main__":
    sys.exit(main())

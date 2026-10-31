"""用合成观测回归并发死锁检测，不连接数据库，也不输出原始锁图。"""

from pathlib import Path
from itertools import product
import shutil
import subprocess
import unittest
from unittest.mock import Mock
import uuid

import deadlock_observation as observation
from integration_errors import IntegrationDeadlineExceeded


def engine_report(deadlock=None, clock="2026-10-28 00:00:00", transactions="old"):
    """deadlock 为合成事务标记；返回含必要章节的引擎报告，不代表真实数据库内容。"""
    report = f"InnoDB\t\t\n{clock} INNODB MONITOR OUTPUT\n"
    if deadlock is not None:
        report += ("------------------------\nLATEST DETECTED DEADLOCK\n------------------------\n"
                   f"*** (1) TRANSACTION:\n{deadlock}\n*** WE ROLL BACK TRANSACTION (1)\n")
    return report + f"------------\nTRANSACTIONS\n------------\n{transactions}\n"


class FakeFixture:
    def __init__(self, path):
        self.path = path
        self.logs = []
        self.version = "8.0.16"
        self.identity = "synthetic-instance"
        self.uptime = 100
        self.global_count = None
        self.metric = ""
        self.engine = engine_report()
        self.engine_error = False
        self.database_generation = 0
        self.calls = []
        self.log = self.path / "hub-1.log"
        self.log.write_bytes(b"")

    def sql(self, query):
        """query 是观察器发出的固定 SQL；返回合成快照，未知 SQL 直接失败。"""
        self.calls.append(query)
        if query.startswith("SELECT @@server_uuid"):
            result = f"{self.identity}\t{self.version}\nUptime\t{self.uptime}"
            if self.global_count is not None:
                result += f"\nInnodb_deadlocks\t{self.global_count}"
            return result
        if query.startswith("SELECT `COUNT`"):
            return self.metric
        if query == "SET GLOBAL innodb_monitor_enable='lock_deadlocks'":
            self.metric = self.metric.replace("disabled", "enabled")
            return ""
        if query == "SHOW ENGINE INNODB STATUS":
            if self.engine_error:
                raise RuntimeError("synthetic-sensitive-query")
            return self.engine
        raise AssertionError("观察器使用了未约定的 SQL")

    def append_log(self, text):
        """text 为合成日志；只追加到本测试的临时文件，无返回值。"""
        with self.log.open("ab") as stream:
            stream.write(text.encode("utf-8"))


class DeadlockObservationTests(unittest.TestCase):
    def setUp(self):
        base = (Path(__file__).resolve().parents[1] / ".local").resolve()
        base.mkdir(exist_ok=True)
        directory = base / ("deadlock-observation-test-" + uuid.uuid4().hex)
        # 普通 mkdir 继承工作区权限，兼容 Windows 下的受限运行账号。
        directory.mkdir()
        self.addCleanup(self.remove_directory, directory, base)
        self.fixture = FakeFixture(directory)

    def remove_directory(self, directory, base):
        """directory 为本测试创建的目录，base 为预期父目录；核对后删除，无返回值。"""
        self.assertEqual(directory.resolve().parent, base)
        self.assertTrue(directory.name.startswith("deadlock-observation-test-"))
        shutil.rmtree(directory)

    def assert_deadlock(self):
        return self.assertRaisesRegex(AssertionError, "^" + observation.DEADLOCK_DETECTED + "$")

    def assert_unavailable(self):
        return self.assertRaisesRegex(AssertionError, "^" + observation.OBSERVATION_UNAVAILABLE + "$")

    def test_empty_metrics_detect_deadlock_after_successful_transaction_retry(self):
        fixture = self.fixture
        with self.assert_deadlock():
            with observation.assert_no_new_deadlocks(fixture):
                fixture.engine = engine_report("synthetic-sensitive-query")
                fixture.append_log("数据库死锁重试，action=accept attempt=1 category=mysql.common-server code=1213\n")
                results = [(200, {"eventId": "1", "duplicate": True}) for _ in range(10)]
                self.assertTrue(all(status == 200 for status, _ in results))

    def test_new_engine_deadlock_is_detected_without_error_path_logs(self):
        fixture = self.fixture
        fixture.metric = "0\tenabled"
        with self.assert_deadlock():
            with observation.assert_no_new_deadlocks(fixture):
                fixture.engine = engine_report("new-synthetic-transaction")
                self.assertEqual(fixture.metric, "0\tenabled", "模拟旧版 MySQL 漏计")

    def test_old_deadlock_and_old_retry_log_do_not_count_again(self):
        fixture = self.fixture
        fixture.engine = engine_report("old-synthetic-transaction")
        fixture.append_log("数据库死锁重试，action=accept attempt=1 category=mysql.common-server code=1213\n")
        with observation.assert_no_new_deadlocks(fixture):
            fixture.engine = engine_report("old-synthetic-transaction", clock="2026-10-28 00:00:01", transactions="new")
            fixture.append_log("本轮操作成功\n")

    def test_new_retry_is_detected_even_if_latest_engine_graph_is_unchanged(self):
        fixture = self.fixture
        fixture.engine = engine_report("old-synthetic-transaction")
        with self.assert_deadlock():
            with observation.assert_no_new_deadlocks(fixture):
                fixture.append_log("数据库死锁重试，action=accept attempt=1 category=mysql.common-server code=1213\n")

    def test_error_path_1213_is_also_detected(self):
        for message in ("处理请求时依赖服务出错", "投递时数据库操作失败", "数据库死锁重试耗尽"):
            with self.subTest(message=message), self.assert_deadlock():
                with observation.assert_no_new_deadlocks(self.fixture):
                    self.fixture.append_log(message + "，category=mysql.common-server code=1213\n")

    def test_reliable_counter_checks_numeric_growth(self):
        fixture = self.fixture
        fixture.version = "9.0.0"
        fixture.global_count = "7"
        with self.assert_deadlock():
            with observation.assert_no_new_deadlocks(fixture):
                fixture.global_count = "8"

    def test_reliable_counter_compares_numbers_instead_of_text(self):
        fixture = self.fixture
        fixture.version = "9.0.0"
        fixture.global_count = "007"
        with observation.assert_no_new_deadlocks(fixture):
            fixture.global_count = "7"

    def test_missing_global_counter_uses_reliable_enabled_metric(self):
        fixture = self.fixture
        fixture.version = "9.0.0"
        fixture.metric = "2\tenabled"
        fixture.engine_error = True
        with observation.assert_no_new_deadlocks(fixture):
            pass
        with self.assert_deadlock():
            with observation.assert_no_new_deadlocks(fixture):
                fixture.metric = "3\tenabled"

    def test_metric_is_enabled_before_the_observation_window(self):
        fixture = self.fixture
        fixture.version = "9.0.0"
        fixture.metric = "0\tdisabled"
        with observation.assert_no_new_deadlocks(fixture):
            self.assertEqual(fixture.metric, "0\tenabled")
        self.assertIn("SET GLOBAL innodb_monitor_enable='lock_deadlocks'", fixture.calls)

    def test_missing_metric_is_not_assumed_zero_when_fallback_is_unavailable(self):
        fixture = self.fixture
        fixture.engine_error = True
        with self.assert_unavailable() as raised:
            with observation.assert_no_new_deadlocks(fixture):
                pass
        self.assertNotIn("synthetic-sensitive-query", str(raised.exception))

    def test_empty_metric_can_use_complete_engine_and_log_observation(self):
        with observation.assert_no_new_deadlocks(self.fixture):
            self.fixture.append_log("本轮回调均已完成\n")

    def test_malformed_engine_report_cannot_replace_a_missing_counter(self):
        self.fixture.engine = "synthetic-sensitive-query"
        with self.assert_unavailable():
            with observation.assert_no_new_deadlocks(self.fixture):
                pass

    def test_other_error_numbers_are_not_counted_as_1213(self):
        with observation.assert_no_new_deadlocks(self.fixture):
            self.fixture.append_log("处理请求时依赖服务出错，category=mysql.common-server code=12130\n")

    def test_missing_identity_at_end_fails_with_fixed_text(self):
        with self.assert_unavailable() as raised:
            with observation.assert_no_new_deadlocks(self.fixture):
                self.fixture.identity = ""
        self.assertEqual(str(raised.exception), observation.OBSERVATION_UNAVAILABLE)

    def test_legacy_zero_needs_engine_and_log_window(self):
        fixture = self.fixture
        fixture.metric = "0\tenabled"
        fixture.log.unlink()
        with self.assert_unavailable():
            with observation.assert_no_new_deadlocks(fixture):
                pass

    def test_log_truncation_makes_fallback_unavailable(self):
        fixture = self.fixture
        fixture.append_log("历史日志\n")
        with self.assert_unavailable():
            with observation.assert_no_new_deadlocks(fixture):
                fixture.log.write_bytes(b"")

    def test_counter_reset_or_database_restart_cannot_claim_no_deadlocks(self):
        for field, value in (("global_count", "0"), ("uptime", 1), ("database_generation", 1),
                             ("identity", "other-instance")):
            with self.subTest(field=field):
                fixture = self.fixture
                fixture.global_count = "7"
                fixture.version = "9.0.0"
                fixture.uptime = 100
                fixture.database_generation = 0
                fixture.identity = "synthetic-instance"
                with self.assert_unavailable():
                    with observation.assert_no_new_deadlocks(fixture):
                        setattr(fixture, field, value)

    def test_body_failure_is_preserved(self):
        original = RuntimeError("synthetic-body-failure")
        with self.assertRaises(RuntimeError) as raised:
            with observation.assert_no_new_deadlocks(self.fixture):
                raise original
        self.assertIs(raised.exception, original)

    def test_observation_deadline_is_preserved_for_each_channel_and_boundary(self):
        cases = product(("identity", "metric", "engine", "log"), ("start", "end"),
                        (TimeoutError, subprocess.TimeoutExpired, IntegrationDeadlineExceeded))
        for channel, boundary, error_type in cases:
            with self.subTest(channel=channel, boundary=boundary, error_type=error_type.__name__):
                fixture = FakeFixture(self.fixture.path)
                original = (subprocess.TimeoutExpired("synthetic-command", 0.1)
                            if error_type is subprocess.TimeoutExpired else error_type("synthetic-deadline"))

                def expire():
                    if channel == "log":
                        fixture.logs.append(Mock(closed=False, flush=Mock(side_effect=original)))
                        return
                    regular_sql = fixture.sql

                    def sql(query):
                        matched = (channel == "identity" and query.startswith("SELECT @@server_uuid")
                                   or channel == "metric" and query.startswith("SELECT `COUNT`")
                                   or channel == "engine" and query == "SHOW ENGINE INNODB STATUS")
                        if matched:
                            raise original
                        return regular_sql(query)

                    fixture.sql = sql

                if boundary == "start":
                    expire()
                with self.assertRaises(error_type) as raised:
                    with observation.assert_no_new_deadlocks(fixture):
                        if boundary == "end":
                            expire()
                self.assertIs(raised.exception, original)

    def test_batch_escaped_engine_output_keeps_historical_graph_stable(self):
        fixture = self.fixture
        fixture.engine = engine_report("old").replace("\n", "\\n")
        with observation.assert_no_new_deadlocks(fixture):
            fixture.engine = engine_report("old", transactions="changed").replace("\n", "\\n")

    def test_counter_reliability_matches_known_fixed_version(self):
        source = "innodb_metrics.lock_deadlocks"
        self.assertEqual(observation.counter_reliability("8.0.16", source), "known_undercount")
        self.assertEqual(observation.counter_reliability("8.0.16", "global_status.Innodb_deadlocks"),
                         "unverified_legacy_counter")
        self.assertEqual(observation.counter_reliability("8.0.18-commercial", source), "no_known_undercount")
        self.assertEqual(observation.counter_reliability("9.0.0", source), "no_known_undercount")
        self.assertEqual(observation.counter_reliability("10.11.0-MariaDB", source), "unverified_version")


if __name__ == "__main__":
    unittest.main()

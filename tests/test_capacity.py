"""容量工具的统计和真实接收端故障测试，不启动 MySQL。"""

from contextlib import closing
import os
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import capacity
import integration


class CapacityTests(unittest.TestCase):
    def test_percentiles_keep_empty_samples_distinct(self):
        self.assertEqual(capacity.distribution([])["p95"], None)
        result = capacity.distribution(range(1, 101))
        self.assertEqual((result["p50"], result["p95"], result["p99"], result["max"]), (50, 95, 99, 100))

    def test_status_snapshot_counts_tasks_instead_of_events(self):
        fixture = Mock()
        fixture.sql.return_value = "0\t4\t2500000\n1\t2\t1000000\n2\t9\t5000000"
        snapshot = capacity.status_snapshot(fixture, "capacity_test_")
        self.assertEqual(snapshot, {"pending": 4, "delivering": 2, "done": 9, "failed": 0, "oldestPendingMs": 2500})
        self.assertIn("JOIN gamehub.hub_event", fixture.sql.call_args.args[0])

    def test_process_sample_reads_only_this_process(self):
        sample = capacity.process_sample(os.getpid())
        if sample is None:
            self.skipTest("当前运行账号不能读取进程资源")
        self.assertGreater(sample["rssBytes"], 0)
        self.assertGreaterEqual(sample["handles"], 0)
        self.assertGreaterEqual(sample["cpuSeconds"], 0)

    def test_overloaded_client_does_not_extend_the_submission_window(self):
        def slow_request(*_args):
            time.sleep(0.2)
            return {"status": 503}
        options = SimpleNamespace(duration_seconds=0.05, rate=100, clients=1, broadcast_every=0)
        with patch.object(capacity, "submit_one", slow_request):
            results, seconds = capacity.run_load(Mock(), options, "synthetic_")
        self.assertEqual(len(results), 1, "负载窗口结束后不能继续发送剩余的排队请求")
        self.assertLess(seconds, 0.6)

    def test_empty_database_and_real_empty_ledger_are_not_valid_load(self):
        fixture = capacity.CapacityFixture(Path("unused-binary"), Path("unused-mysql"))
        self.addCleanup(fixture.close)
        receiver = integration.DemoReceiver(("127.0.0.1", 0), fixture.path / "empty-ledger.db",
                                             fixture.env["GAMEHUB_DEMO_OUTBOUND_SECRET"])
        fixture.receivers.append(receiver)
        fixture.sql = Mock(return_value="")
        with self.assertRaisesRegex(AssertionError, "空负载"):
            capacity.verify_result(fixture, "synthetic_")

    def test_authentication_failure_stops_after_first_request(self):
        fixture = Mock()
        fixture.request.return_value = (401, {})
        with self.assertRaisesRegex(AssertionError, "401"):
            capacity.submit_one(fixture, "synthetic_", 1, 0)
        self.assertEqual(fixture.request.call_count, 1)

    def test_all_non_200_responses_cannot_pass_and_are_classified_individually(self):
        result = capacity.assess_requests([
            {"index": 0, "status": 503, "eventId": None}, {"index": 1, "status": 429, "eventId": None}],
            "synthetic_", {"synthetic_0": "11"})
        self.assertFalse(result["validLoad"])
        self.assertFalse(result["allRequestsHttpConfirmed"])
        self.assertEqual(result["persistedWithoutHttpAckCount"], 1)
        self.assertEqual(result["notObservedInHubAtEndCount"], 1)
        with self.assertRaises(AssertionError):
            capacity.assess_requests([{"index": 0, "status": 200, "eventId": "11"}], "synthetic_", {})

    def test_mixed_confirmed_load_does_not_claim_complete_http_reception(self):
        result = capacity.assess_requests([
            {"index": 0, "status": 200, "eventId": "11"}, {"index": 1, "status": 503, "eventId": None}],
            "synthetic_", {"synthetic_0": "11", "synthetic_1": "12"})
        self.assertTrue(result["validLoad"])
        self.assertFalse(result["allRequestsHttpConfirmed"])
        self.assertEqual(result["httpConfirmedCount"], 1)

    def test_fault_thread_start_failure_still_stops_real_sampler(self):
        fixture = Mock(hubs=[])
        fixture.configure_hubs.return_value = {"pollMs": 1000}
        fixture.sql.side_effect = ["33554432\t30\t1", "9.0"]
        sampler = capacity.Sampler(fixture, "synthetic_")
        sampler.thread = threading.Thread(target=lambda: sampler.stop.wait(3), daemon=True)
        fault = Mock(ident=None, is_alive=Mock(return_value=False))
        original = RuntimeError("synthetic fault thread start")
        fault.start.side_effect = original
        with patch.object(capacity, "Sampler", return_value=sampler), \
                patch.object(capacity.threading, "Thread", return_value=fault), \
                patch.object(capacity, "begin_database_observation"), \
                self.assertRaises(RuntimeError) as result:
            capacity.execute(fixture, SimpleNamespace(scenario="database-outage"), {})
        self.assertIs(result.exception, original)
        self.assertTrue(sampler.stop.is_set())
        self.assertFalse(sampler.thread.is_alive())

    def test_sampler_cleanup_error_does_not_replace_load_failure(self):
        fixture = Mock(hubs=[])
        fixture.sql.side_effect = ["33554432\t30\t1", "9.0"]
        sampler = Mock(stop=threading.Event())
        sampler.close.side_effect = OSError("synthetic cleanup")
        fault = Mock(ident=None, is_alive=Mock(return_value=False))
        original = AssertionError("synthetic load failure")
        report = {}
        with patch.object(capacity, "Sampler", return_value=sampler), \
                patch.object(capacity.threading, "Thread", return_value=fault), \
                patch.object(capacity, "begin_database_observation"), \
                patch.object(capacity, "run_load", side_effect=original), self.assertRaises(AssertionError) as result:
            capacity.execute(fixture, SimpleNamespace(scenario="baseline"), report)
        self.assertIs(result.exception, original)
        self.assertEqual(report["measurementCleanupErrors"], ["OSError"])
        self.assertTrue(sampler.stop.is_set())

    def test_deadlock_delta_marks_restart_instead_of_claiming_zero_errors(self):
        start = {"serverUuid": "private-instance-id", "deadlocks": 7, "uptimeSeconds": 50,
                 "observedMonotonic": 100.0, "generation": 0, "mysqlVersion": "9.0.0",
                 "counterSource": "innodb_metrics.lock_deadlocks", "counterReliability": "no_known_undercount"}
        end = dict(start, deadlocks=10, uptimeSeconds=60, observedMonotonic=110.0)
        result = capacity.deadlock_observation(start, end)
        self.assertEqual(result["delta"], 3)
        self.assertFalse(result["resetDetected"])
        restarted = dict(end, deadlocks=0, uptimeSeconds=5, generation=1)
        result = capacity.deadlock_observation(start, restarted)
        self.assertIsNone(result["delta"])
        self.assertTrue(result["resetDetected"])
        self.assertIn("explicit_database_restart", result["resetReasons"])
        self.assertNotIn("private-instance-id", json.dumps(result))

    def test_missing_global_status_falls_back_to_enabled_innodb_metric(self):
        fixture = Mock()
        fixture.sql.side_effect = ["private-id\t9.0.0\nUptime\t9", "3\tenabled"]
        result = capacity.database_snapshot(fixture)
        self.assertEqual(result["deadlocks"], 3)
        self.assertEqual(result["counterSource"], "innodb_metrics.lock_deadlocks")
        self.assertEqual(result["uptimeSeconds"], 9)

    def test_legacy_metric_zero_is_incomplete_instead_of_no_deadlocks(self):
        fixture = Mock()
        fixture.sql.side_effect = ["private-id\t8.0.16\nUptime\t9", "0\tenabled"]
        start = capacity.database_snapshot(fixture)
        end = dict(start, uptimeSeconds=10, observedMonotonic=start["observedMonotonic"] + 1)
        result = capacity.deadlock_observation(start, end)
        self.assertEqual(result["start"]["mysqlVersion"], "8.0.16")
        self.assertEqual(result["start"]["counterReliability"], "known_undercount")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["delta"])
        self.assertTrue(result["counterKnownUndercount"])
        self.assertEqual(result["counterReliabilityReference"], "https://bugs.mysql.com/bug.php?id=77399")

    def test_counter_reliability_uses_fixed_version_and_exact_source(self):
        source = "innodb_metrics.lock_deadlocks"
        for version in ("5.7.44", "8.0.16", "8.0.17-commercial"):
            with self.subTest(version=version):
                self.assertEqual(capacity.counter_reliability(version, source), "known_undercount")
        for version in ("8.0.18", "8.0.18-commercial", "8.4.7", "9.0.0"):
            with self.subTest(version=version):
                self.assertEqual(capacity.counter_reliability(version, source), "no_known_undercount")
        for version in ("unknown", "10.11.0-MariaDB"):
            with self.subTest(version=version):
                self.assertEqual(capacity.counter_reliability(version, source), "unverified_version")
        self.assertEqual(capacity.counter_reliability("8.0.16", "global_status.Innodb_deadlocks"),
                         "unverified_legacy_counter")

    def test_disabled_metric_is_not_treated_as_zero(self):
        fixture = Mock()
        fixture.sql.side_effect = ["private-id\t9.0.0\nUptime\t9", "0\tdisabled"]
        with self.assertRaises(RuntimeError):
            capacity.database_snapshot(fixture)
        fixture.sql.side_effect = ["private-id\t9.0.0\nUptime\t9", "0\tdisabled", "", "0\tenabled"]
        result = capacity.database_snapshot(fixture, enable_metric=True)
        self.assertEqual(result["deadlocks"], 0)
        self.assertTrue(any("SET GLOBAL innodb_monitor_enable='lock_deadlocks'" in call.args[0]
                            for call in fixture.sql.call_args_list))

    def test_metric_enabled_only_at_end_cannot_claim_complete_observation(self):
        start = {"serverUuid": "private-id", "deadlocks": 0, "uptimeSeconds": 9,
                 "generation": 0, "observedMonotonic": 1.0, "counterSource": "innodb_metrics.lock_deadlocks",
                 "counterReliability": "no_known_undercount", "mysqlVersion": "9.0.0"}
        end = dict(start, uptimeSeconds=10, observedMonotonic=2.0, counterEnabledDuringRead=True)
        result = capacity.deadlock_observation(start, end)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["delta"])
        self.assertEqual(result["reason"], "counter_was_disabled_at_end")

    def test_restart_enables_disabled_metric_before_continuing_load(self):
        fixture = Mock()
        fixture.database_generation = 1
        fixture.sql.side_effect = ["private-id\t9.0.0\nUptime\t1", "0\tdisabled", "", "0\tenabled"]
        capacity.observe_database_restart(fixture)
        self.assertEqual(fixture.capacity_restart_observations, [{"generation": 1, "complete": True,
                         "deadlocks": 0, "counterSource": "innodb_metrics.lock_deadlocks", "mysqlVersion": "9.0.0",
                         "counterReliability": "no_known_undercount"}])
        self.assertTrue(any("SET GLOBAL innodb_monitor_enable='lock_deadlocks'" in call.args[0]
                            for call in fixture.sql.call_args_list))

    def test_restart_remains_visible_when_final_counter_cannot_be_read(self):
        fixture = capacity.CapacityFixture(Path("unused-binary"), Path("unused-mysql"))
        self.addCleanup(fixture.close)
        fixture.database_generation = 1
        fixture.capacity_database_start = {"generation": 0}
        fixture.sql = Mock(side_effect=RuntimeError(fixture.env["GAMEHUB_HUBDB_PASSWORD"]))
        capacity.observe_database_restart(fixture)
        report = {}
        capacity.finish_database_observation(fixture, report)
        result = report["databaseDiagnostics"]
        self.assertFalse(result["deadlocks"]["complete"])
        self.assertTrue(result["deadlocks"]["resetDetected"])
        self.assertIsNone(result["deadlocks"]["delta"])
        self.assertEqual(result["deadlocks"]["resetReasons"], ["explicit_database_restart"])
        self.assertEqual(result["restartCounters"], [{"generation": 1, "complete": False, "errorType": "RuntimeError"}])
        self.assertNotIn(fixture.env["GAMEHUB_HUBDB_PASSWORD"], json.dumps(result))

    def test_absent_status_and_metric_leave_explicit_incomplete_observation(self):
        fixture = capacity.CapacityFixture(Path("unused-binary"), Path("unused-mysql"))
        self.addCleanup(fixture.close)
        fixture.sql = Mock(side_effect=["private-id\t9.0.0\nUptime\t9", ""])
        capacity.begin_database_observation(fixture)
        report = {}
        capacity.finish_database_observation(fixture, report)
        self.assertEqual(report["databaseDiagnostics"]["deadlocks"],
            {"complete": False, "reason": "counter_unavailable_at_start", "errorType": "RuntimeError"})

    def test_log_observation_keeps_only_fixed_categories_and_numeric_codes(self):
        fixture = capacity.CapacityFixture(Path("unused-binary"), Path("unused-mysql"))
        self.addCleanup(fixture.close)
        fixture.sql = Mock(side_effect=["private-id\t9.0.0\nInnodb_deadlocks\t4\nUptime\t50",
                                        "private-id\t9.0.0\nInnodb_deadlocks\t5\nUptime\t50"])
        log = (fixture.path / "hub-observation.log").open("wb")
        fixture.logs.append(log)
        log.write("投递时数据库操作失败，category=mysql.common-server code=999，old\n".encode())
        log.flush()
        capacity.begin_database_observation(fixture)
        secret = fixture.env["GAMEHUB_HUBDB_PASSWORD"]
        log.write(("投递时数据库操作失败，category=mysql.common-server code=1213，" + secret + "\n"
                   "处理请求时依赖服务出错，category=generic code=995 " + fixture.admin() + "\n"
                   "投递时数据库操作失败，category=" + secret + " code=123\n"
                   "投递失败，taskId=81234567890 attempts=1 category=network_error\n"
                   "投递失败，taskId=81234567890 attempts=2 category=" + secret + "\n"
                   "数据库死锁重试，action=" + secret + " attempt=1 category=mysql.common-server code=1213\n"
                   "数据库死锁重试耗尽，action=" + secret + " attempt=3 category=mysql.common-server code=1213\n"
                   "已回收过期的投递租约，count=3\n").encode())
        report = {}
        capacity.finish_database_observation(fixture, report)
        result = report["databaseDiagnostics"]
        self.assertEqual(result["deadlocks"]["delta"], 1)
        errors = result["writtenHubLogs"]["databaseErrors"]
        self.assertEqual(errors, [{"kind": "deadlockExhausted", "category": "mysql.common-server", "code": 1213, "count": 1},
                                  {"kind": "deadlockRetry", "category": "mysql.common-server", "code": 1213, "count": 1},
                                  {"kind": "deliveryStorage", "category": "mysql.common-server", "code": 1213, "count": 1},
                                  {"kind": "requestDependency", "category": "generic", "code": 995, "count": 1}])
        self.assertEqual(result["writtenHubLogs"]["deliveryFailures"], {"network_error": 1})
        self.assertEqual(result["writtenHubLogs"]["fixedCounts"], {"deliveryStorageUnclassified": 1,
            "recoveredLeaseTasks": 3, "deadlockRetries": 1, "deadlockExhausted": 1})
        text = json.dumps(result)
        for forbidden in (secret, fixture.admin(), "private-id", "81234567890", "code=999"):
            self.assertNotIn(forbidden, text)

    def test_database_observation_failure_is_explicit_and_does_not_echo_exception(self):
        fixture = capacity.CapacityFixture(Path("unused-binary"), Path("unused-mysql"))
        self.addCleanup(fixture.close)
        fixture.capacity_database_start = {"unused": True}
        fixture.sql = Mock(side_effect=RuntimeError(fixture.env["GAMEHUB_HUBDB_PASSWORD"]))
        report = {}
        capacity.finish_database_observation(fixture, report)
        self.assertEqual(report["databaseDiagnostics"]["deadlocks"], {"complete": False, "errorType": "RuntimeError"})
        self.assertNotIn(fixture.env["GAMEHUB_HUBDB_PASSWORD"], json.dumps(report))

    def test_receiver_outage_reopens_same_endpoint_and_preserves_database(self):
        fixture = capacity.CapacityFixture(Path("unused-binary"), Path("unused-mysql"))
        self.addCleanup(fixture.close)
        receiver = integration.DemoReceiver(("127.0.0.1", 0), fixture.path / "receiver.db",
                                             fixture.env["GAMEHUB_DEMO_OUTBOUND_SECRET"])
        capacity.track_connections(receiver)
        fixture._start_receiver(receiver)
        address = receiver.server_address
        connection = socket.create_connection(address, timeout=2)
        self.addCleanup(connection.close)
        integration.wait_for(lambda: bool(receiver.capacity_connections), "接收端接受保活连接", 2)
        report = {}
        capacity.apply_fault(fixture, SimpleNamespace(scenario="target-outage", duration_seconds=0.03,
                                                      fault_seconds=0.01), "unused_", report, capacity.threading.Event())
        self.assertNotIn("faultFailureType", report)
        self.assertEqual(report["fault"]["kind"], "target-outage")
        self.assertEqual(len(fixture.receivers), 1)
        self.assertIsNot(fixture.receivers[0], receiver)
        self.assertEqual(fixture.receivers[0].server_address, address)
        self.assertEqual(connection.recv(1), b"", "失联时旧保活连接也应断开")
        with closing(socket.create_connection(address, timeout=2)):
            pass


if __name__ == "__main__":
    unittest.main()

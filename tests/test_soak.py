"""验证长稳工具的内存上界、统计和失败清理，不启动已有数据库。"""

import argparse
from contextlib import closing
import importlib.util
from pathlib import Path
import os
import shutil
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

SPEC = importlib.util.spec_from_file_location("soak", Path(__file__).with_name("soak.py"))
SOAK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SOAK)


class SoakTests(unittest.TestCase):
    def test_online_aggregation_has_fixed_storage(self):
        summary = SOAK.Summary(sample_limit=3)
        for index in range(10000):
            summary.observe(200, index + 1)
            summary.sample({"seconds": index, "resource": {"rssBytes": index, "handles": 12}})
        report = summary.report()
        self.assertEqual(report["requests"], 10000)
        self.assertEqual(sum(item["count"] for item in report["latencyHistogramMs"]), 10000)
        self.assertEqual(len(summary.latency_bins), 32)
        self.assertEqual(len(report["recentSamples"]), 3)
        self.assertEqual(report["peakRssBytes"], 9999)

    def test_fixture_cleanup_keeps_original_failure(self):
        class Fixture:
            def __init__(self, *args):
                self.run_deadline = None
                self.failure = None
            def start(self):
                raise ValueError("合成初始化失败")
            def close(self, failure):
                self.failure = failure
        fixture = Fixture()
        options = argparse.Namespace(binary=Path("synthetic.exe"), mysql_bin=Path("synthetic-mysql"),
                                     setup_seconds=10, duration_seconds=10, drain_seconds=10)
        with self.assertRaisesRegex(ValueError, "合成初始化失败"):
            SOAK.run(options, lambda *args: fixture)
        self.assertIsInstance(fixture.failure, ValueError)
        self.assertIsNone(fixture.run_deadline)

    def test_success_also_closes_independent_fixture(self):
        class Fixture:
            closed = False
            def start(self):
                pass
            def close(self, failure):
                self.closed = True
                self.failure = failure
        fixture = Fixture()
        options = argparse.Namespace(binary=Path("synthetic.exe"), mysql_bin=Path("synthetic-mysql"),
                                     setup_seconds=10, duration_seconds=10, drain_seconds=10)
        with patch.object(SOAK, "run_load", return_value={"passed": True}):
            self.assertEqual(SOAK.run(options, lambda *args: fixture), {"passed": True})
        self.assertTrue(fixture.closed)
        self.assertIsNone(fixture.failure)

    def test_cleanup_error_does_not_replace_original_failure(self):
        class Fixture:
            def start(self):
                raise ValueError("合成原始初始化失败")
            def close(self, failure):
                raise RuntimeError("合成清理失败")
        options = argparse.Namespace(binary=Path("synthetic.exe"), mysql_bin=Path("synthetic-mysql"),
                                     setup_seconds=10, duration_seconds=10, drain_seconds=10)
        with self.assertRaisesRegex(ValueError, "合成原始初始化失败"):
            SOAK.run(options, lambda *args: Fixture())

    def test_load_requires_ledger_for_every_persisted_event(self):
        directory = Path(__file__).resolve().parents[1] / ".local" / ("soak-unit-" + uuid.uuid4().hex)
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory))
        database = directory / "ledger.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE synthetic_ledger (game_id TEXT,server_id INTEGER,namespace_key TEXT,"
                               "biz_key TEXT,amount_minor INTEGER,PRIMARY KEY(game_id,server_id,namespace_key,biz_key))")
            connection.commit()
        class Fixture:
            sent = 0
            write_ledger = True
            hub = SimpleNamespace(pid=os.getpid())
            receivers = [SimpleNamespace(database=database, server_id=1)]
            def operation_timeout(self, maximum):
                return maximum
            def request(self, method, path, value, **kwargs):
                self.sent += 1
                if self.write_ledger:
                    with closing(sqlite3.connect(database)) as connection:
                        connection.execute("INSERT INTO synthetic_ledger VALUES('demo',1,'demo',?,?)",
                                           (value["channelOrderId"], value["amountMinor"]))
                        connection.commit()
                return 200, {}
            def sql(self, query):
                return str(self.sent)
        options = argparse.Namespace(sample_limit=2, duration_seconds=0.02, rate=1, clients=1,
                                     sample_interval=30, drain_seconds=1)
        fixture = Fixture()
        with patch.object(SOAK, "status_snapshot", side_effect=lambda *args: {
                "pending": 0, "delivering": 0, "done": fixture.sent, "failed": 0}), \
                patch.object(SOAK, "sample_tasks", return_value={"pending": 0}), \
                patch.object(SOAK, "process_sample", return_value=None):
            report = SOAK.run_load(fixture, options)
        self.assertTrue(report["passed"])
        fixture.sent = 0
        fixture.write_ledger = False
        with patch.object(SOAK, "status_snapshot", side_effect=lambda *args: {
                "pending": 0, "delivering": 0, "done": fixture.sent, "failed": 0}), \
                patch.object(SOAK, "sample_tasks", return_value={"pending": 0}), \
                patch.object(SOAK, "process_sample", return_value=None):
            report = SOAK.run_load(fixture, options)
        self.assertFalse(report["passed"])

    def test_same_count_wrong_identity_cannot_pass(self):
        directory = Path(__file__).resolve().parents[1] / ".local" / ("soak-unit-" + uuid.uuid4().hex)
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory))
        database = directory / "ledger.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE synthetic_ledger (game_id TEXT,server_id INTEGER,namespace_key TEXT,"
                               "biz_key TEXT,amount_minor INTEGER,PRIMARY KEY(game_id,server_id,namespace_key,biz_key))")
            connection.execute("INSERT INTO synthetic_ledger VALUES('demo',1,'demo','soak-test-0-wrong',100)")
            connection.commit()
        fixture = SimpleNamespace(receivers=[SimpleNamespace(database=database, server_id=1)],
                                  operation_timeout=lambda maximum: maximum)
        self.assertEqual(SOAK.verify_ledger(fixture, "soak-test-", 1), (1, False))
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE synthetic_ledger SET biz_key='soak-test-0'")
            connection.commit()
        self.assertEqual(SOAK.verify_ledger(fixture, "soak-test-", 1), (1, True))

    def test_sample_rejects_missing_or_denied_observations(self):
        fixture = SimpleNamespace(admin=lambda **kwargs: "synthetic-viewer",
                                  request=lambda *args, **kwargs: (403, {}))
        with self.assertRaises(ValueError):
            SOAK.sample_tasks(fixture)
        fixture.request = lambda *args, **kwargs: (200, {"pending": 0})
        with self.assertRaises(ValueError):
            SOAK.sample_tasks(fixture)


if __name__ == "__main__":
    unittest.main()

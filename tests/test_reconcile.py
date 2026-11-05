"""验证两段对账能区分缺记录、确认丢失、金额冲突和重复流水。"""

import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reconcile", ROOT / "scripts/reconcile.py")
RECONCILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECONCILE)


def row(order_id="synthetic-order", **values):
    return {"gameId": "demo", "channel": "synthetic", "channelOrderId": order_id,
            "serverId": 1, "amountMinor": 100, "currency": "CNY", "status": "", **values}


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.local = ROOT / ".local"
        self.local.mkdir(exist_ok=True)
        self.directory = self.local / ("reconcile-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(self.remove_files)

    def remove_files(self):
        self.assertEqual(self.directory.resolve().parent, self.local.resolve())
        shutil.rmtree(self.directory)

    def test_matched_order_has_no_issue(self):
        self.assertEqual(RECONCILE.reconcile([row()], [row(status="done")], [row()])["issues"], [])

    def test_confirmation_lost_is_different_from_missing_ledger(self):
        report = RECONCILE.reconcile([row(), row("synthetic-no-ledger")],
            [row(status="failed"), row("synthetic-no-ledger", status="done")], [row()])
        self.assertEqual(report["categoryCounts"], {"hub_done_without_ledger": 1,
                                                    "ledger_committed_confirmation_unresolved": 1})

    def test_channel_hub_and_ledger_missing_records_are_separate(self):
        report = RECONCILE.reconcile([row("channel-only")], [row("hub-only", status="pending")], [row("ledger-only")])
        self.assertEqual(set(report["categoryCounts"]), {"channel_missing_in_hub", "hub_missing_in_channel",
                                                         "ledger_missing_in_hub", "hub_without_ledger"})

    def test_duplicate_and_conflicting_values_are_both_reported(self):
        report = RECONCILE.reconcile([row()], [row(status="done")], [row(), row(amountMinor=200)])
        self.assertEqual(report["categoryCounts"], {"amount_currency_or_target_conflict": 1, "duplicate_record": 1})
        self.assertNotIn("synthetic-order", json.dumps(report))
        self.assertEqual(report["issues"][0]["source"], "ledger")

    def test_structured_identity_does_not_merge_concatenation_collisions(self):
        first = ("ab", "c", "d")
        second = ("a", "bc", "d")
        self.assertNotEqual(RECONCILE.reference(first), RECONCILE.reference(second))

    def test_json_and_csv_use_same_integer_rules(self):
        json_file = self.directory / "records.json"
        json_file.write_text(json.dumps([row()]), encoding="utf-8")
        csv_file = self.directory / "records.csv"
        csv_file.write_text("gameId,channel,channelOrderId,serverId,amountMinor,currency,status\n"
                            "demo,synthetic,synthetic-order,1,100,CNY,\n", encoding="utf-8")
        self.assertEqual(RECONCILE.load_records(json_file), RECONCILE.load_records(csv_file))
        for amount, currency in ((True, "CNY"), (1.5, "CNY"), (-1, "CNY"), (100, "$CN")):
            json_file.write_text(json.dumps([row(amountMinor=amount, currency=currency)]), encoding="utf-8")
            with self.assertRaises(ValueError):
                RECONCILE.load_records(json_file)
        json_file.write_text(json.dumps([row()]), encoding="utf-8")
        with self.assertRaises(ValueError):
            RECONCILE.load_records(json_file, require_status=True)

    def test_demo_is_synthetic_and_does_not_overwrite_existing_files(self):
        RECONCILE.write_demo(self.directory)
        report = RECONCILE.reconcile(*(RECONCILE.load_records(self.directory / name)
            for name in ("channel.json", "hub.json", "ledger.json")))
        self.assertEqual(report["categoryCounts"], {"amount_currency_or_target_conflict": 1,
            "channel_missing_in_hub": 1, "ledger_committed_confirmation_unresolved": 1})
        with self.assertRaises(ValueError):
            RECONCILE.write_demo(self.directory)

    def test_output_cannot_overwrite_input_directly_or_through_hardlink(self):
        RECONCILE.write_demo(self.directory)
        inputs = [self.directory / name for name in ("channel.json", "hub.json", "ledger.json")]
        before = [path.read_bytes() for path in inputs]
        command = [sys.executable, str(ROOT / "scripts/reconcile.py"), "--channel", str(inputs[0]),
                   "--hub", str(inputs[1]), "--ledger", str(inputs[2]), "--output"]
        for source in inputs:
            result = subprocess.run(command + [str(source)], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual([path.read_bytes() for path in inputs], before)
        alias = self.directory / "report-hardlink.json"
        os.link(inputs[0], alias)
        result = subprocess.run(command + [str(alias)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([path.read_bytes() for path in inputs], before)

    def test_hub_identity_isolates_namespace_and_target(self):
        first = {"gameId": "demo", "taskType": "payment", "namespace": "merchant-a", "bizKey": "order",
                 "serverId": 1, "amountMinor": 100, "currency": "CNY", "status": "done"}
        second = dict(first, namespace="merchant-b")
        report = RECONCILE.reconcile([first, second], [first, second], [first])
        self.assertEqual(report["identityMode"], "hub-v2")
        self.assertEqual(report["categoryCounts"], {"hub_done_without_ledger": 1})
        self.assertNotEqual(RECONCILE.identity(first), RECONCILE.identity(dict(first, serverId=2)))

    def test_legacy_and_hub_identity_cannot_mix(self):
        modern = {"gameId": "demo", "taskType": "payment", "namespace": "merchant", "bizKey": "order", "serverId": 1}
        with self.assertRaises(ValueError):
            RECONCILE.reconcile([row()], [modern], [])

    def test_complete_window_required_for_export_envelope(self):
        metadata = {"windowStart": "2026-11-01T00:00:00Z", "windowEnd": "2026-11-02T00:00:00Z",
                    "watermark": "2026-11-02T01:00:00Z"}
        path = self.directory / "export.json"
        path.write_text(json.dumps({"formatVersion": 2, **metadata, "records": [row(status="done")]}), encoding="utf-8")
        self.assertEqual(RECONCILE.load_export(path)[1], metadata)
        with self.assertRaises(ValueError):
            RECONCILE.validate_window(dict(metadata, watermark="2026-11-01T20:00:00Z"))

    def test_case_ledger_repeated_run_preserves_disposition_and_count(self):
        report = RECONCILE.reconcile([row()], [], [])
        database = self.directory / "cases.sqlite"
        RECONCILE.record_cases(database, report)
        issue = report["issues"][0]
        RECONCILE.record_cases(database, None, (issue["reference"], issue["category"], "resolved"))
        RECONCILE.record_cases(database, report)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT state,observations FROM cases").fetchone(), ("resolved", 1))
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone(), (1,))
        self.assertNotIn("synthetic-order", database.read_bytes().decode("utf-8", errors="ignore"))


if __name__ == "__main__":
    unittest.main()

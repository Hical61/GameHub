"""备份演练必须包含投递观测、运行控制和配置回执的非空业务数据。"""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_drill", ROOT / "scripts/backup_drill.py")
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class SeedFixture:
    """模拟已隔离的 Fixture 接口，记录种子覆盖范围。"""

    def __init__(self, missing=None):
        self.requests = []
        self.queries = []
        self.hub = object()
        self.stopped = False
        self.missing = missing

    def request(self, method, path, body=None, **kwargs):
        """按路由返回种子结果。"""
        self.requests.append((method, path, body))
        if path.startswith("/admin/control/"):
            return 200, {"revision": "1"}
        if path.startswith("/admin/archive/"):
            return 200, {"archivedTasks": 1, "redactedEvents": 1}
        return 200, {"eventId": str(len(self.requests))}

    def sql(self, query):
        """仅返回固定合成结构版本和非空检查结果。"""
        self.queries.append(query)
        if "MAX(version)" in query:
            return "4"
        if "COUNT(*)" in query:
            return "0" if self.missing and self.missing in query else "1"
        return ""

    def admin(self):
        """返回合成令牌占位。"""
        return "synthetic"

    def tasks(self, event):
        """所有合成种子投递均完成。"""
        return [2]

    def _stop_child(self, child):
        self.stopped = True


class BackupSeedTests(unittest.TestCase):
    def test_seed_uses_apis_and_checks_operation_tables(self):
        fixture = SeedFixture()
        with patch.object(BACKUP.integration, "wait_for", side_effect=lambda predicate, *args: predicate()):
            BACKUP.seed(fixture)
        paths = [item[1] for item in fixture.requests]
        self.assertIn("/admin/control/demo", paths)
        self.assertIn("/internal/config-applied/demo/1", paths)
        for name in ("delivery_attempt", "game_delivery_control", "config_server_state"):
            self.assertTrue(any("COUNT(*) FROM gamehub." + name in query for query in fixture.queries))
        self.assertTrue(fixture.stopped)

    def test_empty_operation_table_is_not_accepted_as_restore_coverage(self):
        fixture = SeedFixture(missing="config_server_state")
        with patch.object(BACKUP.integration, "wait_for", side_effect=lambda predicate, *args: predicate()):
            with self.assertRaises(AssertionError):
                BACKUP.seed(fixture)


if __name__ == "__main__":
    unittest.main()

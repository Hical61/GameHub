"""检查集成测试失败时是否留下诊断，以及清理会不会盖住原来的错误。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import integration


class FixtureCleanupTests(unittest.TestCase):
    """使用临时合成文件和短命进程，不启动 MySQL，也不连接现有数据库。"""

    def setUp(self):
        self.fixture = integration.Fixture(Path("unused-gamehub"), Path("unused-mysql"))
        self.addCleanup(self.remove_test_files)

    def remove_test_files(self):
        for child in self.fixture.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        for log in self.fixture.logs:
            if not log.closed:
                log.close()
        for path in (self.fixture.path, self.fixture.diagnostic_path):
            if path and path.exists():
                self.assertEqual(path.resolve().parent, self.fixture.base)
                shutil.rmtree(path)

    def prepare_failure(self):
        fixture = self.fixture
        fixture.sql = Mock(return_value="1\tpending\t2026-10-28 00:00:00")
        fixture._ready_snapshot = Mock(return_value={"status": 503})
        receiver = Mock(server_id=2, requests=7, pause=None, lock=threading.Lock())
        fixture.receivers.append(receiver)
        fixture.receiver_threads[receiver] = Mock(is_alive=Mock(return_value=True))
        return receiver

    def start_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.fixture.children.append(child)
        self.fixture.hub = child
        return child

    def test_success_stops_child_and_removes_workdir(self):
        child = self.start_child()
        (self.fixture.path / "gamehub.json").write_text("temporary", encoding="utf-8")
        self.fixture.close()
        self.assertIsNotNone(child.poll())
        self.assertFalse(self.fixture.path.exists())
        self.assertIsNone(self.fixture.diagnostic_path)

    def test_failure_keeps_sanitized_diagnostics_and_stops_processes(self):
        fixture = self.fixture
        receiver = self.prepare_failure()
        child = self.start_child()
        secret = fixture.env["GAMEHUB_ADMIN_JWT_SECRET"]
        admin_token = fixture.admin()
        log = (fixture.path / "hub.log").open("wb")
        fixture.logs.append(log)
        log.write(("test output " + secret + " " + admin_token + "\n").encode())
        (fixture.path / "gamehub.json").write_text(secret, encoding="utf-8")
        data = fixture.path / "mysql-data"
        data.mkdir()
        (data / "private-state").write_text(secret, encoding="utf-8")
        fixture.responses.append({"method": "POST", "path": "/api/callback/demo/demo", "status": 503, "bodyBytes": 30})
        fixture.sql.side_effect = lambda _: self.assert_child_running(child) or "1\tpending"
        output = io.StringIO()
        with redirect_stdout(output):
            fixture.close(failure=AssertionError(secret))
        self.assertIsNotNone(child.poll())
        receiver.shutdown.assert_called_once()
        receiver.server_close.assert_called_once()
        self.assertFalse(fixture.path.exists())
        files = {path.name for path in fixture.diagnostic_path.iterdir()}
        self.assertEqual(files, {"hub.log", "diagnostics.json"})
        report = json.loads((fixture.diagnostic_path / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(report["ready"], {"status": 503})
        self.assertEqual(report["receivers"], [{"serverId": 2, "requests": 7}])
        self.assertEqual(report["recentResponses"][0]["status"], 503)
        self.assertEqual(report["failureType"], "AssertionError")
        text = output.getvalue() + "".join(path.read_text(encoding="utf-8") for path in fixture.diagnostic_path.iterdir())
        for name in ("GAMEHUB_HUBDB_PASSWORD", "GAMEHUB_ADMIN_JWT_SECRET",
                     "GAMEHUB_DEMO_CALLBACK_SECRET", "GAMEHUB_DEMO_OUTBOUND_SECRET"):
            self.assertNotIn(fixture.env[name], text)
        self.assertNotIn(admin_token, text)

    def assert_child_running(self, child):
        self.assertIsNone(child.poll(), "诊断必须在停进程前采集")

    def test_log_size_limit_never_keeps_secret_fragments(self):
        fixture = self.fixture
        self.prepare_failure()
        max_bytes = 512 * 1024
        samples = (fixture.env["GAMEHUB_ADMIN_JWT_SECRET"], fixture.admin())
        for index, secret in enumerate(samples):
            encoded = secret.encode()
            prefix = b"older log\ncredential="
            head = prefix + encoded + b"\n"
            # 让 512 KiB 的切口恰好落在密钥或令牌中间。
            start = len(prefix) + len(encoded) // 2
            suffix_size = max_bytes + start - len(head)
            ending = b"\nsafe line\n"
            data = head + b"x" * (suffix_size - len(ending)) + ending
            (fixture.path / f"boundary-{index}.log").write_bytes(data)
        partial = fixture.env["GAMEHUB_DEMO_OUTBOUND_SECRET"][:24]
        (fixture.path / "partial.log").write_text("safe line\ncredential=" + partial, encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            fixture.close(failure=AssertionError("synthetic"))
        for index, secret in enumerate(samples):
            text = (fixture.diagnostic_path / f"boundary-{index}.log").read_text(encoding="utf-8")
            self.assertNotIn(secret[len(secret) // 2:], text)
            self.assertTrue(text.endswith("safe line\n"))
        self.assertEqual((fixture.diagnostic_path / "partial.log").read_text(encoding="utf-8"), "safe line\n")

    def test_failed_query_does_not_prevent_other_diagnostics_or_cleanup(self):
        fixture = self.fixture
        self.prepare_failure()
        fixture.sql.side_effect = RuntimeError(fixture.env["GAMEHUB_HUBDB_PASSWORD"])
        with redirect_stdout(io.StringIO()):
            fixture.close(failure=AssertionError("synthetic"))
        report = json.loads((fixture.diagnostic_path / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(report["tasks"], {"errorType": "RuntimeError"})
        self.assertEqual(report["databaseProcesses"], {"errorType": "RuntimeError"})
        self.assertEqual(report["ready"], {"status": 503})
        self.assertFalse(fixture.path.exists())

    def test_failed_archive_keeps_original_files_but_still_stops_child(self):
        fixture = self.fixture
        self.prepare_failure()
        child = self.start_child()
        output = io.StringIO()
        with patch.object(fixture, "save_diagnostics", side_effect=OSError("synthetic")), \
                redirect_stdout(output), redirect_stderr(output):
            fixture.close(failure=AssertionError("synthetic"))
        self.assertIsNotNone(child.poll())
        self.assertTrue(fixture.path.exists())
        self.assertIn("原临时目录仍保留", output.getvalue())

    def test_one_receiver_failure_does_not_skip_other_cleanup(self):
        fixture = self.fixture
        receiver = self.prepare_failure()
        receiver.shutdown.side_effect = RuntimeError("synthetic")
        other = Mock(server_id=3, requests=0, pause=None, lock=threading.Lock())
        fixture.receivers.append(other)
        fixture.receiver_threads[other] = Mock(is_alive=Mock(return_value=True))
        child = self.start_child()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            fixture.close(failure=AssertionError("synthetic"))
        receiver.server_close.assert_called_once()
        other.shutdown.assert_called_once()
        other.server_close.assert_called_once()
        self.assertIsNotNone(child.poll())
        self.assertTrue((fixture.diagnostic_path / "diagnostics.json").exists())

    def test_receiver_thread_start_failure_cannot_hang_cleanup(self):
        # 放进有超时的子进程；回归时即便 shutdown 挂住，也不会卡死整套测试。
        script = """
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[2])
import integration
integration.ROOT = Path(sys.argv[1])
fixture = integration.Fixture(Path('unused-gamehub'), Path('unused-mysql'))
receiver = integration.DemoReceiver(('127.0.0.1', 0), fixture.path / 'receiver.db', 'x' * 32)
with patch.object(integration.threading.Thread, 'start', side_effect=RuntimeError('synthetic')):
    try:
        fixture._start_receiver(receiver)
    except RuntimeError:
        pass
    else:
        raise AssertionError('thread.start should fail')
fixture.close()
assert not fixture.path.exists()
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.fixture.path),
                                 str(Path(integration.__file__).parent)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_original_failure_survives_cleanup_error(self):
        original = AssertionError("original synthetic failure")
        fixture = Mock()
        fixture.start.side_effect = original
        fixture.close.side_effect = OSError("cleanup synthetic failure")
        with redirect_stderr(io.StringIO()), self.assertRaises(AssertionError) as result:
            integration.run_fixture(fixture)
        self.assertIs(result.exception, original)
        fixture.close.assert_called_once_with(failure=original)

    def test_cleanup_error_after_success_is_reported(self):
        fixture = Mock()
        fixture.close.side_effect = RuntimeError("cleanup synthetic failure")
        with patch.object(integration, "verify"), self.assertRaises(RuntimeError):
            integration.run_fixture(fixture)
        fixture.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

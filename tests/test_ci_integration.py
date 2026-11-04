"""检查整轮时限和可控失败是否仍会取证、关进程并保留原错误。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import time
import unittest
from unittest.mock import Mock, call, patch

import integration


class IntegrationDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.Fixture(Path("unused-gamehub"), Path("unused-mysql"))
        self.addCleanup(self.cleanup)

    def cleanup(self):
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

    def test_total_deadline_preserves_diagnostics_and_stops_real_child(self):
        fixture = self.fixture
        fixture.sql = Mock(side_effect=RuntimeError("synthetic"))
        fixture._ready_snapshot = Mock(return_value={"status": 503})

        def start():
            fixture.hub = fixture.launch([sys.executable, "-c", "import time; time.sleep(30)"], "hub")
            integration.wait_for(lambda: False, "synthetic wait", timeout=20)

        fixture.start = start
        started = time.monotonic()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(integration.IntegrationDeadlineExceeded):
            integration.run_fixture(fixture, timeout_seconds=0.2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(all(child.poll() is not None for child in fixture.children))
        self.assertFalse(fixture.path.exists())
        report = json.loads((fixture.diagnostic_path / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failureType"], "IntegrationDeadlineExceeded")
        self.assertIsNone(fixture.run_deadline)

    def test_injected_failure_skips_verify_and_keeps_the_failure(self):
        fixture = Mock()
        with patch.object(integration, "verify") as verify, redirect_stderr(io.StringIO()), \
                self.assertRaises(AssertionError) as result:
            integration.run_fixture(fixture, inject_failure=True)
        verify.assert_not_called()
        fixture.close.assert_called_once_with(failure=result.exception)
        self.assertIsNone(fixture.run_deadline)

    def test_leaving_context_clears_deadline_for_cleanup(self):
        with integration.RunDeadline(0.1) as deadline:
            self.assertGreater(deadline.timeout(10), 0)
            self.assertLessEqual(deadline.timeout(10), 0.1)
        time.sleep(0.15)
        self.assertIsNone(integration.ACTIVE_DEADLINE.get())
        integration.wait_for(lambda: True, "cleanup after deadline")

    def test_deadline_does_not_replace_an_existing_test_error(self):
        original = AssertionError("synthetic original")
        fixture = Mock()
        fixture.start.side_effect = original
        with redirect_stderr(io.StringIO()), self.assertRaises(AssertionError) as result:
            integration.run_fixture(fixture, timeout_seconds=5)
        self.assertIs(result.exception, original)
        fixture.close.assert_called_once_with(failure=original)

    def test_deadline_after_process_creation_registers_and_stops_real_child(self):
        fixture = self.fixture
        fixture.sql = Mock(side_effect=RuntimeError("synthetic"))
        fixture._ready_snapshot = Mock(return_value={"status": 503})
        original_popen = integration.subprocess.Popen
        created = []
        def create(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            created.append(child)
            fixture.run_deadline.deadline = time.monotonic() - 1
            return child
        fixture.start = lambda: fixture.launch([sys.executable, "-c", "import time; time.sleep(30)"], "hub")
        with patch.object(integration.subprocess, "Popen", side_effect=create), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(integration.IntegrationDeadlineExceeded):
            integration.run_fixture(fixture, timeout_seconds=5)
        self.assertEqual(len(created), 1)
        self.assertIn(created[0], fixture.children)
        self.assertIsNotNone(created[0].poll(), "已创建的进程必须登记并被清理")

    def test_invalid_deadline_is_rejected_before_fixture_start(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                integration.RunDeadline(value)

    def test_mysql_uses_utf8mb4_and_keeps_chinese_output_intact(self):
        fixture = self.fixture
        expected = "合成公告"
        result = Mock(returncode=0, stdout=(expected + "\n").encode("utf-8"), stderr=b"")
        with patch.object(integration.subprocess, "run", return_value=result) as run:
            self.assertEqual(fixture.sql("SELECT '合成公告'"), expected)
        self.assertIn("--default-character-set=utf8mb4", run.call_args.args[0])
        self.assertIn("合成公告".encode("utf-8"), run.call_args.kwargs["input"])

    def test_sql_error_diagnostic_never_keeps_statement_or_parameters(self):
        fixture = self.fixture
        secret = fixture.env["GAMEHUB_HUBDB_PASSWORD"]
        result = Mock(returncode=1, stdout=b"", stderr=("ERROR 1406 (22001) at line 3: " + secret + "\n").encode())
        with patch.object(integration.subprocess, "run", return_value=result), self.assertRaises(RuntimeError) as error:
            fixture.sql("SELECT '" + secret + "'")
        self.assertEqual(list(fixture.sql_errors), [{"code": 1406, "sqlState": "22001", "line": 3}])
        self.assertNotIn(secret, str(error.exception) + json.dumps(list(fixture.sql_errors)))


class IntegrationSuiteTests(unittest.TestCase):
    def test_default_command_runs_isolated_suites_with_remaining_budget(self):
        core, multi = Mock(), Mock()
        now = [100.0]
        def elapsed(*_args, **_kwargs):
            now[0] += 100
        argv = ["integration.py", "--binary", "unused-hub", "--mysql-bin", "unused-mysql"]
        with patch.object(sys, "argv", argv), \
                patch.object(integration, "Fixture", return_value=core) as core_factory, \
                patch.object(integration, "MultiGameFixture", return_value=multi) as multi_factory, \
                patch.object(integration.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(integration, "run_fixture", side_effect=elapsed) as run, \
                redirect_stdout(io.StringIO()):
            integration.main()
        self.assertEqual(run.call_args_list, [
            call(core, 600, False, verifier=integration.verify),
            call(multi, 500, False, verifier=integration.verify_multi_game)])
        core_factory.assert_called_once_with(Path("unused-hub"), Path("unused-mysql"))
        multi_factory.assert_called_once_with(Path("unused-hub"), Path("unused-mysql"))

    def test_selected_suite_does_not_create_the_other_instance(self):
        for suite, active in (("core", 0), ("multi-game", 1)):
            with self.subTest(suite=suite), \
                    patch.object(integration, "Fixture") as core_factory, \
                    patch.object(integration, "MultiGameFixture") as multi_factory, \
                    patch.object(integration, "run_fixture") as run, redirect_stdout(io.StringIO()):
                integration.run_suites(Path("unused-hub"), Path("unused-mysql"), suite=suite)
                factories = (core_factory, multi_factory)
                factories[active].assert_called_once()
                factories[1 - active].assert_not_called()
                self.assertIs(run.call_args.args[0], factories[active].return_value)

    def test_expired_budget_does_not_create_the_next_instance(self):
        now = [0.0]
        def expire(*_args, **_kwargs):
            now[0] = 2.0
        with patch.object(integration, "Fixture"), \
                patch.object(integration, "MultiGameFixture") as multi_factory, \
                patch.object(integration.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(integration, "run_fixture", side_effect=expire), \
                self.assertRaises(integration.IntegrationDeadlineExceeded):
            integration.run_suites(Path("unused-hub"), Path("unused-mysql"), timeout_seconds=1)
        multi_factory.assert_not_called()

    def test_later_suite_failure_keeps_its_diagnostics_and_original_error(self):
        core, multi = Mock(), Mock()
        original = AssertionError("synthetic multi-game failure")
        with patch.object(integration, "Fixture", return_value=core), \
                patch.object(integration, "MultiGameFixture", return_value=multi), \
                patch.object(integration, "verify") as verify, \
                patch.object(integration, "verify_multi_game", side_effect=original) as verify_multi, \
                redirect_stdout(io.StringIO()), self.assertRaises(AssertionError) as error:
            integration.run_suites(Path("unused-hub"), Path("unused-mysql"))
        self.assertIs(error.exception, original)
        verify.assert_called_once_with(core)
        verify_multi.assert_called_once_with(multi)
        core.close.assert_called_once_with()
        multi.close.assert_called_once_with(failure=original)
        self.assertIsNone(multi.run_deadline)
        self.assertIsNone(integration.ACTIVE_DEADLINE.get())


if __name__ == "__main__":
    unittest.main()

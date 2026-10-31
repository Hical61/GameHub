"""测试 MySQL 下载校验和解压边界，不访问网络或启动数据库。"""

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mysql_setup", ROOT / "scripts/setup-test-mysql.py")
SETUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETUP)


class DownloadResponse(io.BytesIO):
    def __init__(self, content, declared=None):
        super().__init__(content)
        self.headers = {} if declared is None else {"Content-Length": str(declared)}
        self.fp = Mock()


class MysqlSetupTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / ".local" / ("ci-mysql-setup-" + uuid.uuid4().hex)
        self.path.mkdir(parents=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        resolved = self.path.resolve()
        self.assertEqual(resolved.parent, (ROOT / ".local").resolve())
        shutil.rmtree(resolved)

    def archive(self, entries):
        path = self.path / SETUP.ARCHIVE_NAME
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return path

    def test_corrupt_cached_zip_is_rejected_without_network_or_unpack(self):
        (self.path / SETUP.ARCHIVE_NAME).write_bytes(b"not the verified archive")
        with patch.object(SETUP.urllib.request, "urlopen") as request, self.assertRaises(ValueError):
            SETUP.download_archive(self.path)
        request.assert_not_called()
        self.assertFalse((self.path / SETUP.ARCHIVE_ROOT).exists())

    def test_bad_download_is_not_saved_as_cache(self):
        with patch.object(SETUP.urllib.request, "urlopen", return_value=DownloadResponse(b"wrong bytes")), \
                self.assertRaises(ValueError):
            SETUP.download_archive(self.path)
        self.assertFalse((self.path / SETUP.ARCHIVE_NAME).exists())
        self.assertFalse((self.path / (SETUP.ARCHIVE_NAME + ".part")).exists())

    def test_download_is_verified_then_reused(self):
        content = b"synthetic verified archive"
        response = DownloadResponse(content, len(content))
        with patch.object(SETUP, "ARCHIVE_SHA256", hashlib.sha256(content).hexdigest()), \
                patch.object(SETUP, "ARCHIVE_BYTES", len(content)), \
                patch.object(SETUP.urllib.request, "urlopen", return_value=response) as request:
            archive = SETUP.download_archive(self.path)
            self.assertEqual(archive.read_bytes(), content)
            self.assertEqual(SETUP.download_archive(self.path), archive)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(response.fp.raw._sock.settimeout.called)

    def test_download_rejects_declared_or_streamed_oversize_before_saving_it(self):
        for declared in (10, None):
            with self.subTest(declared=declared), patch.object(SETUP, "ARCHIVE_BYTES", 3), \
                    patch.object(SETUP.urllib.request, "urlopen", return_value=DownloadResponse(b"oversized!", declared)), \
                    self.assertRaises(ValueError):
                SETUP.download_archive(self.path)
            self.assertFalse((self.path / SETUP.ARCHIVE_NAME).exists())
            self.assertFalse((self.path / (SETUP.ARCHIVE_NAME + ".part")).exists())

    def test_slow_download_uses_one_total_deadline(self):
        response = DownloadResponse(b"abc")
        with patch.object(SETUP, "ARCHIVE_BYTES", 3), \
                patch.object(SETUP.urllib.request, "urlopen", return_value=response) as request, \
                patch.object(SETUP.time, "monotonic", side_effect=(0, 0, 481, 481)), \
                self.assertRaises(RuntimeError):
            SETUP.download_archive(self.path)
        self.assertEqual(request.call_count, 1)
        self.assertFalse((self.path / (SETUP.ARCHIVE_NAME + ".part")).exists())

    def test_verified_zip_cannot_escape_expected_directory(self):
        for name in ("../escape", "other-root/escape", SETUP.ARCHIVE_ROOT + "/../escape",
                     SETUP.ARCHIVE_ROOT + "/bad\\escape", SETUP.ARCHIVE_ROOT + "/C:escape"):
            with self.subTest(name=name):
                archive = self.archive({name: b"unexpected"})
                with patch.object(SETUP, "ARCHIVE_SHA256", SETUP.sha256(archive)), self.assertRaises(ValueError):
                    SETUP.unpack_archive(archive, self.path)
                self.assertFalse((self.path / SETUP.ARCHIVE_ROOT).exists())
                self.assertEqual([item.name for item in self.path.iterdir()], [SETUP.ARCHIVE_NAME])

    def test_valid_tools_are_installed_and_existing_unmarked_files_are_not_overwritten(self):
        entries = {SETUP.ARCHIVE_ROOT + "/bin/mysql.exe": b"synthetic mysql",
                   SETUP.ARCHIVE_ROOT + "/bin/mysqld.exe": b"synthetic mysqld"}
        archive = self.archive(entries)
        with patch.object(SETUP, "ARCHIVE_SHA256", SETUP.sha256(archive)):
            binary_directory = SETUP.unpack_archive(archive, self.path)
            self.assertEqual((binary_directory / "mysql.exe").read_bytes(), b"synthetic mysql")
            self.assertEqual(SETUP.unpack_archive(archive, self.path), binary_directory)
            (binary_directory.parent / "gamehub-source.sha256").unlink()
            with self.assertRaises(ValueError):
                SETUP.unpack_archive(archive, self.path)
            self.assertEqual((binary_directory / "mysql.exe").read_bytes(), b"synthetic mysql")

    def rename_paths(self):
        source = self.path / "rename-source"
        source.mkdir()
        binary = source / "mysql.exe"
        binary.write_bytes(b"synthetic mysql")
        return source, self.path / "rename-target", binary

    @unittest.skipUnless(os.name == "nt", "需要 Windows 文件句柄语义")
    def test_directory_rename_recovers_after_real_child_handle_is_closed(self):
        source, target, binary = self.rename_paths()
        held = binary.open("rb")
        failed = threading.Event()
        original_rename = Path.rename
        errors = []
        def observed_rename(current, destination):
            try:
                return original_rename(current, destination)
            except PermissionError as error:
                errors.append(error.winerror)
                failed.set()
                raise
        def close_after_failure():
            failed.wait(timeout=2)
            held.close()
        closer = threading.Thread(target=close_after_failure)
        closer.start()
        try:
            with patch.object(Path, "rename", observed_rename):
                SETUP.rename_directory(source, target)
        finally:
            failed.set()
            closer.join(timeout=3)
            held.close()
        self.assertFalse(closer.is_alive())
        self.assertIn(5, errors)
        self.assertFalse(source.exists())
        self.assertEqual((target / binary.name).read_bytes(), b"synthetic mysql")

    @unittest.skipUnless(os.name == "nt", "需要 Windows 文件句柄语义")
    def test_persistent_child_handle_keeps_original_error_and_bounded_wait(self):
        source, target, binary = self.rename_paths()
        original_rename = Path.rename
        real_sleep = time.sleep
        with binary.open("rb"), patch.object(Path, "rename", autospec=True, side_effect=original_rename) as rename, \
                patch.object(SETUP.time, "sleep", wraps=real_sleep) as pause, self.assertRaises(PermissionError) as error:
            SETUP.rename_directory(source, target)
        self.assertEqual(error.exception.winerror, 5)
        self.assertEqual(rename.call_count, 6)
        self.assertAlmostEqual(sum(call.args[0] for call in pause.call_args_list), 0.75)
        self.assertTrue(source.exists())
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == "nt", "需要 Windows 文件句柄语义")
    def test_target_created_during_handle_denial_is_never_overwritten(self):
        source, target, binary = self.rename_paths()
        original_rename = Path.rename
        def create_target_after_denial(current, destination):
            try:
                return original_rename(current, destination)
            except PermissionError:
                target.mkdir()
                (target / "existing.txt").write_text("preserve", encoding="utf-8")
                raise
        with binary.open("rb"), patch.object(Path, "rename", create_target_after_denial), self.assertRaises(FileExistsError):
            SETUP.rename_directory(source, target)
        self.assertEqual((target / "existing.txt").read_text(encoding="utf-8"), "preserve")
        self.assertTrue(source.exists())

    def test_non_windows_permission_error_is_not_retried(self):
        source, target, _ = self.rename_paths()
        error = PermissionError("synthetic denial")
        error.winerror = 5
        with patch.object(SETUP.sys, "platform", "linux"), patch.object(Path, "rename", side_effect=error) as rename, \
                patch.object(SETUP.time, "sleep") as pause, self.assertRaises(PermissionError) as captured:
            SETUP.rename_directory(source, target)
        self.assertIs(captured.exception, error)
        rename.assert_called_once()
        pause.assert_not_called()

    def test_unpack_cleanup_failure_does_not_replace_original_rename_error(self):
        archive = self.archive({SETUP.ARCHIVE_ROOT + "/bin/mysql.exe": b"synthetic mysql",
                                SETUP.ARCHIVE_ROOT + "/bin/mysqld.exe": b"synthetic mysqld"})
        error = PermissionError("synthetic persistent denial")
        with patch.object(SETUP, "ARCHIVE_SHA256", SETUP.sha256(archive)), \
                patch.object(SETUP, "rename_directory", side_effect=error), \
                patch.object(SETUP.shutil, "rmtree", side_effect=OSError("synthetic cleanup failure")), \
                patch.object(SETUP.sys, "stderr", new_callable=io.StringIO) as warnings, \
                self.assertRaises(PermissionError) as captured:
            SETUP.unpack_archive(archive, self.path)
        self.assertIs(captured.exception, error)
        self.assertIn("临时解压目录未能清理", warnings.getvalue())

    def test_unrelated_windows_error_is_not_retried(self):
        source, target, _ = self.rename_paths()
        error = PermissionError("synthetic unrelated failure")
        error.winerror = 87
        with patch.object(SETUP.sys, "platform", "win32"), patch.object(Path, "rename", side_effect=error) as rename, \
                patch.object(SETUP.time, "sleep") as pause, self.assertRaises(PermissionError) as captured:
            SETUP.rename_directory(source, target)
        self.assertIs(captured.exception, error)
        rename.assert_called_once()
        pause.assert_not_called()


if __name__ == "__main__":
    unittest.main()

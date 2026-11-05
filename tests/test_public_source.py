"""用隔离 Git 工作树验证公开源码副本边界，不操作项目暂存区或真实私有内容。"""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_source_check", ROOT / "scripts/check-public-source.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class PublicSourceTests(unittest.TestCase):
    """所有源文件、Git 索引和输出都限制在本测试新建的 .local 目录内。"""

    def setUp(self):
        self.base = ROOT / ".local" / ("public-source-test-" + uuid.uuid4().hex)
        self.source = self.base / "source"
        self.output = self.base / "export"
        self.source.mkdir(parents=True)
        self.git("init", "--quiet")
        self.write(".gitignore", "/private/\n/.local/\n/build*/\n*.log\n*.bak\n")
        self.write("README.md", "# 合成公开源码\n")
        self.write("CMakeLists.txt", "cmake_minimum_required(VERSION 3.28)\nproject(Synthetic LANGUAGES CXX)\n"
                   "add_executable(synthetic src/main.cpp)\n")
        self.write("src/main.cpp", "int main() { return 0; }\n")

    def tearDown(self):
        resolved = self.base.resolve()
        if resolved.parent != (ROOT / ".local").resolve() or not resolved.name.startswith("public-source-test-"):
            raise RuntimeError("测试清理目录越界")
        def remove_readonly(function, filename, _error):
            target = Path(filename)
            if not target.resolve().is_relative_to(resolved):
                raise RuntimeError("测试清理文件越界")
            target.chmod(stat.S_IWRITE)
            function(filename)

        shutil.rmtree(resolved, onerror=remove_readonly)

    def git(self, *arguments):
        """只对测试私有 Git 仓库操作，失败不输出输入内容。"""
        result = subprocess.run(["git", "-C", str(self.source), *arguments], capture_output=True)
        self.assertEqual(result.returncode, 0, "合成 Git 操作失败")
        return result.stdout

    def write(self, relative, text):
        """写入合成输入，固定为 UTF-8 和 LF。"""
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def prepare(self):
        """准备副本，禁止测试过程把任何命中文本写入标准输出。"""
        with redirect_stdout(io.StringIO()):
            return CHECK.prepare_source(self.source, self.output)

    def test_worktree_bytes_untracked_files_and_manifest_without_private(self):
        self.git("add", "src/main.cpp")
        self.write("src/main.cpp", "int main() { return 7; }\n")
        self.write("src/NewPublic.cpp", "void newPublic() {}\n")
        self.write("private/player-state.json", '{"syntheticSensitive":true}\n')
        self.write(".local/report.log", "synthetic diagnostic\n")
        self.write("build-native/private-output.txt", "synthetic artifact\n")
        self.write("docs/architecture.md", "# 公开架构\n")
        result = self.prepare()
        self.assertEqual(result["status"], "prepared")
        self.assertEqual((self.output / "src/main.cpp").read_text("utf-8"), "int main() { return 7; }\n")
        self.assertTrue((self.output / "src/NewPublic.cpp").is_file())
        self.assertTrue((self.output / "docs/architecture.md").is_file())
        for name in ("private", ".local", "build-native", ".git"):
            self.assertFalse((self.output / name).exists())
        manifest = json.loads((self.output / CHECK.MANIFEST).read_text("utf-8"))
        actual = {path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(actual, {entry["path"] for entry in manifest["files"]} | {CHECK.MANIFEST})
        for entry in manifest["files"]:
            data = (self.output / entry["path"]).read_bytes()
            self.assertEqual((entry["size"], entry["sha256"]), (len(data), hashlib.sha256(data).hexdigest()))
        self.assertEqual(manifest["validation"]["independentBuild"], "not-run")
        self.assertNotIn(str(self.source), json.dumps(manifest))
        # 故意 staged 旧字节，而副本必须来自工作树；真实项目索引从未被操作。
        self.assertIn(b"return 0", self.git("show", ":src/main.cpp"))

    def test_force_tracked_private_and_unignored_backup_are_rejected_before_copy(self):
        self.write("private/hidden.cpp", "void syntheticPrivate() {}\n")
        self.git("add", "--force", "private/hidden.cpp")
        with self.assertRaises(CHECK.SourceError):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.git("rm", "--cached", "--force", "private/hidden.cpp")
        self.write("src/forgotten.zip", "synthetic archive\n")
        with self.assertRaises(CHECK.SourceError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_unknown_root_and_nested_private_cannot_be_exported(self):
        for relative in ("unreviewed/notes.txt", "src/Private/Secret.cpp"):
            with self.subTest(relative=relative):
                path = self.write(relative, "synthetic\n")
                with self.assertRaises(CHECK.SourceError):
                    self.prepare()
                self.assertFalse(self.output.exists())
                path.unlink()

    def test_cross_platform_aliases_and_path_escape_are_rejected(self):
        for relative in ("src/../private/file.cpp", "src//file.cpp", "src/file.cpp.", "src/file.cpp ",
                         "src/CON.cpp", "src/com1", "src/file.cpp:stream", "src/new\nfile.cpp", "/src/file.cpp"):
            with self.subTest(relative=relative):
                self.assertFalse(CHECK.public_path(relative))

    def test_static_missing_private_cmake_and_include_dependencies_fail(self):
        original = (self.source / "CMakeLists.txt").read_text("utf-8")
        for dependency in ("src/Missing.cpp", "private/Private.cpp", "../private/Outside.cpp"):
            with self.subTest(dependency=dependency):
                self.write("CMakeLists.txt", original + "target_sources(synthetic PRIVATE " + dependency + ")\n")
                with self.assertRaises(CHECK.SourceError):
                    self.prepare()
                self.assertFalse(self.output.exists())
        self.write("CMakeLists.txt", original)
        for include in ('"../private/Secret.h"', '"Missing.h"', '<private/Secret.h>'):
            self.write("src/main.cpp", "#include " + include + "\nint main() {}\n")
            with self.assertRaises(CHECK.SourceError):
                self.prepare()

    def test_rooted_public_headers_and_custom_host_relative_root_are_allowed(self):
        self.write("src/core/Public.h", "#pragma once\n")
        self.write("src/main.cpp", '#include "core/Public.h"\n#include <asio/HttpClient.h>\nint main() {}\n')
        self.write("examples/custom-host/CMakeLists.txt",
                   'add_subdirectory(../..)\nadd_executable(host main.cpp)\n'
                   'target_link_libraries(host PRIVATE GameHub::service)\n'
                   'add_subdirectory("${GAMEHUB_SOURCE_DIR}" "${CMAKE_BINARY_DIR}/gamehub")\n')
        self.write("examples/custom-host/main.cpp", "int main() {}\n")
        self.prepare()
        self.assertTrue((self.output / "examples/custom-host/main.cpp").exists())

    def test_readme_private_link_and_content_secret_never_exported_or_echoed(self):
        self.write("README.md", "[合成私有链接](private/guide.md)\n")
        self.write("private/guide.md", "synthetic private documentation\n")
        with self.assertRaises(CHECK.SourceError):
            self.prepare()
        self.write("README.md", "# 公开说明\n")
        secret = "gh" + "p_" + "Z" * 36
        self.write("src/main.cpp", '// ' + secret + "\nint main() {}\n")
        capture = io.StringIO()
        with redirect_stdout(capture), self.assertRaises(CHECK.SourceError):
            CHECK.prepare_source(self.source, self.output)
        self.assertNotIn(secret, capture.getvalue())
        self.assertFalse(self.output.exists())

    def test_deleted_worktree_file_is_not_restored_from_index(self):
        path = self.write("src/Removed.h", "#pragma once\n")
        self.git("add", "src/Removed.h")
        path.unlink()
        self.prepare()
        self.assertFalse((self.output / "src/Removed.h").exists())

    def test_source_output_and_existing_directory_boundaries(self):
        self.output.mkdir()
        sentinel = self.output / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        for output in (self.output, self.source, self.base, self.source / "src/new-copy",
                       self.source / ".local/public-source-check"):
            with self.subTest(output=output), self.assertRaises(CHECK.SourceError):
                CHECK.output_path(self.source, output)
        self.assertEqual(sentinel.read_text("utf-8"), "keep")
        allowed = self.source / ".local/public-source-check/fresh-copy"
        self.assertEqual(CHECK.output_path(self.source, allowed), allowed)

    def test_symbolic_file_and_parent_escape_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "main.cpp").write_text("synthetic secret\n", encoding="utf-8")
        link = self.source / "src/link.cpp"
        try:
            link.symlink_to(outside / "main.cpp")
        except OSError:
            self.skipTest("当前 Windows 会话未授予符号链接权限")
        with self.assertRaises(CHECK.SourceError):
            self.prepare()
        link.unlink()
        parent = self.base / "linked-parent"
        parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(CHECK.SourceError):
            CHECK.output_path(self.source, parent / "export")
        with self.assertRaises(CHECK.SourceError):
            CHECK.snapshot_file(self.base, "linked-parent/main.cpp")

    def test_hardlink_and_reparse_point_rejected(self):
        target = self.base / "outside.cpp"
        target.write_text("synthetic private\n", encoding="utf-8")
        linked = self.source / "src/link.cpp"
        os.link(target, linked)
        with self.assertRaises(CHECK.SourceError):
            self.prepare()
        info = type("ReparseInfo", (), {"st_mode": stat.S_IFDIR,
                                        "st_file_attributes": CHECK.REPARSE_POINT})()
        with patch.object(Path, "lstat", return_value=info), self.assertRaises(CHECK.SourceError):
            CHECK.no_links(self.source)

    def test_git_symlink_mode_rejected_when_worktree_is_ordinary_link_text(self):
        self.git("config", "core.symlinks", "false")
        relative = "docs/link.rtf"
        path = self.write(relative, "../private/hidden.rtf")
        blob = self.git("hash-object", "-w", relative).strip().decode("ascii")
        self.git("update-index", "--add", "--cacheinfo", "120000", blob, relative)
        self.assertFalse(path.is_symlink())
        # 资源后缀不会因缺 LF 被内容检查误挡；必须由 Git 类型边界拒绝。
        self.assertEqual(CHECK.POLICY.check_file(relative, path.read_bytes(), {relative}), [])
        with self.assertRaisesRegex(CHECK.SourceError, "Git 候选类型"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_git_submodule_mode_rejected_even_when_worktree_is_regular_file(self):
        relative = "src/module.cpp"
        path = self.write(relative, "// synthetic ordinary worktree file\n")
        self.git("update-index", "--add", "--cacheinfo", "160000", "1" * 40, relative)
        self.assertTrue(path.is_file())
        with self.assertRaisesRegex(CHECK.SourceError, "Git 候选类型"):
            self.prepare()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == "nt", "只在 Windows 验证实际 junction")
    def test_windows_junction_escape_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "main.cpp").write_text("synthetic private\n", encoding="utf-8")
        junction = self.source / "src/junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, "创建合成 junction 失败")
        with self.assertRaises(CHECK.SourceError):
            CHECK.snapshot_file(self.source, "src/junction/main.cpp")
        with self.assertRaises(CHECK.SourceError):
            CHECK.output_path(self.source, junction / "export")
        self.assertEqual((outside / "main.cpp").read_text("utf-8"), "synthetic private\n")

    def test_limits_prevent_large_or_unbounded_snapshot(self):
        with patch.object(CHECK, "MAX_FILES", 1), self.assertRaises(CHECK.SourceError):
            self.prepare()
        with patch.object(CHECK, "MAX_FILE_BYTES", 4), self.assertRaises(CHECK.SourceError):
            self.prepare()
        with patch.object(CHECK, "MAX_TOTAL_BYTES", 4), self.assertRaises(CHECK.SourceError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_preflight_failure_does_not_create_output_or_success_manifest(self):
        self.write("src/broken.py", "def broken(:\n")
        with self.assertRaises(CHECK.SourceError):
            self.prepare()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()

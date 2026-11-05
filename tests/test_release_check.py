"""用假凭据和临时 Git 仓库测试发布检查，不改项目的暂存区。"""

import base64
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
SPEC = importlib.util.spec_from_file_location("release_check", ROOT / "scripts/check-release.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ContentChecks(unittest.TestCase):
    """用内存中的测试文件，检查报错信息会不会带出原文。"""

    def test_credentials_never_echo(self):
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"sub":"synthetic"}').decode().rstrip("=")
        samples = (
            ("ghp_" + "A" * 36, "疑似 GitHub 令牌"),
            ("github_pat_" + "B" * 70, "疑似 GitHub 令牌"),
            ("sk-" + "proj-" + "C" * 40, "疑似 OpenAI 密钥"),
            ("AK" + "IA" + "D" * 16, "疑似 AWS 访问标识"),
            ("AK" + "ID" + "F" * 32, "疑似腾讯云访问标识"),
            (header + "." + payload + "." + "E" * 43, "疑似 JWT 字面量"),
            ("-----BEGIN " + "RSA PRIVATE KEY-----", "疑似私钥"),
            ("mysql://reader:" + "synthetic-password@localhost/db", "连接地址含嵌入式账号密码"),
        )
        for secret, category in samples:
            with self.subTest(category=category):
                issues = CHECK.check_file("sample.txt", (secret + "\n").encode(), {"sample.txt"})
                self.assertIn(("sample.txt", 1, category), issues)
                output = io.StringIO()
                with redirect_stdout(output):
                    CHECK.report_issues(issues)
                self.assertNotIn(secret, output.getvalue())
                self.assertIn(category, output.getvalue())

    def test_hashes_environment_and_tool_paths_are_allowed(self):
        text = hashlib.sha256(b"abc").hexdigest() + "\n"
        text += "mysql://reader:" + "${DATABASE_PASSWORD}@localhost/db\n"
        text += "C:/vcpkg/scripts/buildsystems/vcpkg.cmake\n"
        text += "C:/Program Files/MySQL/MySQL Server 9.0/bin\n"
        self.assertEqual(CHECK.check_file("example.txt", text.encode(), {"example.txt"}), [])

    def test_generated_paths_and_examples(self):
        rejected = (".local/data.txt", "nested/build-msvc/data.txt", "_deps/lib/source.h", "out/log.txt", "vcpkg_installed/include/a.h",
                    ".vs/settings.json", "node_modules/a.js", "x/__pycache__/a.pyc", ".env", ".env.production",
                    "config/gamehub.json", "config/runtime.local.json", "config/runtime.local.yml", "CMakeUserPresets.json",
                    ".aws/credentials", ".ssh/config", "config/credentials.json", "config/secrets.yaml", "service-account.json",
                    ".netrc", ".git-credentials", "backups/gamehub.sql", "exports/players.csv", "data/raw/2026/players.json",
                    "gamehub-backup.sql", "players_export.csv", "config.json.bak", "config.json~",
                    "tls/server.pem", "store.sqlite3-wal", "lib/libsample.so.1", "deps.zip",
                    "private/docs/notes.md", "private/adapters/SyntheticAdapter.cpp", "private/config/runtime.json",
                    "nested/Private/notes.md", "docs/architecture.md.bak")
        for path in rejected:
            with self.subTest(path=path):
                self.assertTrue(CHECK.forbidden_path(path))
        for path in ("scripts/build-release.py", "migrations/001_initial.sql", "migrations/003_backup_table.sql",
                     "examples/players.sample.csv", "examples/players-export.sample.csv", "config/credentials.example.json", "config/gamehub.example.json",
                     ".env.example", ".env.sample", "src/core/Domain.cpp", "docs/README.md", "docs/architecture.md"):
            with self.subTest(path=path):
                self.assertFalse(CHECK.forbidden_path(path))

    def test_static_configuration_credentials_never_echo(self):
        secret = "synthetic-release-password"
        cases = (
            ("settings.json", json.dumps({"MYSQL_PASSWORD": secret}) + "\n", 1),
            ("settings.json", '{\n  "GAMEHUB_ADMIN_JWT_SECRET": ' + json.dumps(secret) + '\n}\n', 2),
            ("settings.json.example", json.dumps({"GAMEHUB_HUBDB_PASSWORD": secret}) + "\n", 1),
            ("deploy/settings.yml", "environment:\n  MYSQL_PASSWORD: " + secret + " # 注释\n", 2),
            ("deploy/settings.yaml", "settings: {SecretKey: '" + secret + "', enabled: true}\n", 1),
            ("deploy/settings.yaml", "SecretKey: >-\n  " + secret + "\n", 1),
            ("deploy/settings.yaml", "MYSQL_PASSWORD:\n  " + secret + "\n", 1),
            ("deploy/settings.yaml", "MYSQL_PASSWORD: # 下一行是配置值\n  " + secret + "\n", 1),
            ("deploy/settings.yaml", '- "MYSQL_PASSWORD": "' + secret + '"\n', 1),
            ("deploy/settings.yaml", "environment:\n  - MYSQL_PASSWORD=" + secret + "\n", 2),
            (".env.example", "export GAMEHUB_HUBDB_PASSWORD='" + secret + "'\n", 1),
            ("settings.env", "GAMEHUB_ADMIN_JWT_SECRET=" + secret + "\n", 1),
            ("settings.json", '{"password": 123456}\n', 1),
            (".env.sample", "MYSQL_PASSWORD=,synthetic\n", 1),
        )
        for path, text, line in cases:
            with self.subTest(path=path, line=line):
                issues = CHECK.check_file(path, text.encode(), {path})
                self.assertIn((path, line, "配置中含静态凭据"), issues)
                output = io.StringIO()
                with redirect_stdout(output):
                    CHECK.report_issues(issues)
                self.assertNotIn(secret, output.getvalue())

    def test_environment_references_and_pure_placeholders_are_allowed(self):
        values = ("", "${MYSQL_PASSWORD}", "${MYSQL_PASSWORD:?请设置运行账号密码}", "${MYSQL_PASSWORD:-}",
                  "${{ secrets.MYSQL_PASSWORD }}", "${{ secrets.PASSWORD || secrets.FALLBACK_PASSWORD }}",
                  "${MYSQL_PASSWORD:-${OTHER_PASSWORD}}", "${MYSQL_PASSWORD:-change-me}", "$env:MYSQL_PASSWORD", "$MYSQL_PASSWORD", "%MYSQL_PASSWORD%",
                  "<your-password>", "replace-me", "CHANGE_ME", "your_password_here", "example", "REDACTED", "***")
        for value in values:
            for path, text in (("settings.json", json.dumps({"MYSQL_PASSWORD": value}) + "\n"),
                               ("settings.yml", "MYSQL_PASSWORD: '" + value + "'\n"),
                               (".env.example", "MYSQL_PASSWORD='" + value + "'\n")):
                with self.subTest(path=path, value=value):
                    self.assertEqual(CHECK.check_file(path, text.encode(), {path}), [])
        for text in ("password: ${PASSWORD:?a # comment}\n", "password: >-\n  ${PASSWORD}\n",
                     "settings: {password: ${PASSWORD}, enabled: true}\n", "password: null\n", "password: # 环境提供\n",
                     "MYSQL_PASSWORD:\n  ${MYSQL_PASSWORD:-${OTHER_PASSWORD}}\n",
                     "MYSQL_PASSWORD: # 下一行是动态值\n  ${{ secrets.PASSWORD || secrets.FALLBACK_PASSWORD }}\n"):
            self.assertEqual(CHECK.check_file("settings.yml", text.encode(), {"settings.yml"}), [])
        for value in ("${MYSQL_PASSWORD}suffix", "${MYSQL_PASSWORD},suffix", '"${MYSQL_PASSWORD}"suffix'):
            issues = CHECK.check_file(".env.example", ("MYSQL_PASSWORD=" + value + "\n").encode(), {".env.example"})
            self.assertIn("配置中含静态凭据", {item[2] for item in issues})

    def test_static_fallbacks_inside_environment_expressions_are_rejected(self):
        values = ("${PASSWORD:-synthetic-fallback}", "${PASSWORD+synthetic-alternative}", "${PASSWORD:+synthetic-alternative}",
                  "${{ secrets.PASSWORD || 'synthetic-fallback' }}")
        for value in values:
            for path, text in (("settings.json", json.dumps({"MYSQL_PASSWORD": value}) + "\n"),
                               ("settings.yml", 'MYSQL_PASSWORD: "' + value + '"\n'),
                               (".env.example", "MYSQL_PASSWORD=" + value + "\n")):
                with self.subTest(path=path, value=value):
                    self.assertIn((path, 1, "配置中含静态凭据"), CHECK.check_file(path, text.encode(), {path}))

    def test_source_fixtures_variable_names_and_public_vectors_are_allowed(self):
        source = ('payload = \'{"MYSQL_PASSWORD": "synthetic-fixture-password"}\'\n'
                  'password = fixture.env["GAMEHUB_HUBDB_PASSWORD"]\n'
                  'secret = os.environ.get("GAMEHUB_ADMIN_JWT_SECRET", "")\n'
                  'hmacVector = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"\n')
        self.assertEqual(CHECK.check_file("tests/fixture.py", source.encode(), {"tests/fixture.py"}), [])
        text = json.dumps({"jwtSecretEnv": "GAMEHUB_ADMIN_JWT_SECRET", "passwordEnv": "MYSQL_PASSWORD", "secretKeyFile": "key-path"}) + "\n"
        self.assertEqual(CHECK.check_file("settings.json", text.encode(), {"settings.json"}), [])
        yaml = 'description: "password: synthetic-not-an-assignment"\n# MYSQL_PASSWORD: comment\n'
        self.assertEqual(CHECK.check_file("settings.yml", yaml.encode(), {"settings.yml"}), [])

    def test_parser_and_git_errors_never_echo_input(self):
        secret = "synthetic-error-payload"
        with patch.object(CHECK.json, "loads", side_effect=ValueError(secret)):
            issues = CHECK.check_file("settings.json", b'{"password":"synthetic"}\n', {"settings.json"})
        self.assertIn(("settings.json", 1, "JSON 语法错误"), issues)
        output = io.StringIO()
        with patch.object(CHECK, "git_bytes", side_effect=ValueError(secret)), patch.object(CHECK.sys, "argv", ["check-release.py"]):
            with redirect_stdout(output):
                self.assertEqual(CHECK.main(), 1)
        self.assertIn("无法读取 Git 文件集合", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_rtf_preserves_license_bytes_but_scans_recognizable_credentials(self):
        data = b"{\\rtf1 synthetic license\r\n}"
        self.assertEqual(CHECK.check_file("licenses/example.rtf", data, {"licenses/example.rtf"}), [])
        secret = ("ghp_" + "G" * 36).encode()
        issues = CHECK.check_file("licenses/example.rtf", data + secret, {"licenses/example.rtf"})
        self.assertEqual(issues, [("licenses/example.rtf", 2, "疑似 GitHub 令牌")])

    def test_encoding_and_syntax_categories(self):
        cases = (("a.txt", b"\xff\n", "不是有效 UTF-8"), ("a.txt", b"a\r\n", "换行符不是 LF"),
                 ("a.txt", b"a", "缺少末尾换行"), ("a.json", b'{"x":}\n', "JSON 语法错误"),
                 ("a.json", b'{"x":NaN}\n', "JSON 语法错误"), ("a.py", b"def broken(:\n", "Python 语法错误"))
        for path, data, category in cases:
            with self.subTest(category=category):
                issues = CHECK.check_file(path, data, {path})
                self.assertIn(category, {item[2] for item in issues})

    def test_links_resolve_against_publish_set(self):
        published = {"README.md", "docs/guide.md", "LICENSE", "docs/topic/index.md"}
        text = ("[guide](docs/guide.md#part)\n[dir](docs/topic/)\n[up](../LICENSE)\n"
                "[ignored](.local/private.md)\n[ref]: docs/missing.md\n"
                "```markdown\n[example](missing.md)\n```\n")
        issues = CHECK.check_file("README.md", text.encode(), published)
        self.assertEqual({item[1] for item in issues}, {3, 4, 5})
        self.assertTrue(CHECK.local_link_exists("docs/guide.md", "../LICENSE", published))
        self.assertTrue(CHECK.local_link_exists("README.md", "https://example.invalid", published))

    def test_machine_paths_are_reported(self):
        text = "F:/" + "MyCode/GameHub/config.json\n"
        self.assertIn("机器专有绝对路径", {item[2] for item in CHECK.check_file("notes.md", text.encode(), {"notes.md"})})


class GitChecks(unittest.TestCase):
    """用临时仓库测试暂存内容和忽略规则，不执行被检查的代码。"""

    def setUp(self):
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        # Python 3.13 的私有临时目录会挡住 Windows 受限令牌，这里让测试目录继承工作区权限。
        self.root = (local / ("release-check-" + uuid.uuid4().hex)).resolve()
        self.assertEqual(self.root.parent, local.resolve())
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")

    def cleanup(self):
        # 递归删除前再检查一次路径，只清理本次测试目录。
        self.assertEqual(self.root.parent, (ROOT / ".local").resolve())
        def remove_readonly(function, path, _error):
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(self.root, onerror=remove_readonly)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())

    def test_default_uses_index_blob_and_all_files_uses_worktree(self):
        secret = "ghp_" + "F" * 36
        self.write("settings.txt", secret + "\n")
        self.git("add", "settings.txt")
        self.write("settings.txt", "safe\n")
        self.assertTrue(CHECK.check_repository(self.root))
        self.assertEqual(CHECK.check_repository(self.root, all_files=True), [])
        self.git("add", "settings.txt")
        self.write("settings.txt", secret + "\n")
        self.assertEqual(CHECK.check_repository(self.root), [])
        self.assertTrue(CHECK.check_repository(self.root, all_files=True))

    def test_ignored_link_and_force_added_artifact(self):
        self.write(".gitignore", ".local/\n")
        self.write(".local/private.md", "synthetic\n")
        private_adapter = ".local/private-adapters/SyntheticAdapter.cpp"
        self.write(private_adapter, "// 合成私有适配器。\n")
        self.write("README.md", "[local](.local/private.md)\n")
        self.git("add", ".")
        staged = CHECK.git_paths(self.root, "ls-files", "--cached", "-z")
        self.assertTrue({".local/private.md", private_adapter}.isdisjoint(staged))
        issues = CHECK.check_repository(self.root, all_files=True)
        self.assertIn(("README.md", 1, "本地链接目标未纳入发布文件"), issues)
        self.git("add", "--force", ".local/private.md", private_adapter)
        for all_files in (False, True):
            with self.subTest(all_files=all_files):
                issues = CHECK.check_repository(self.root, all_files=all_files)
                for path in (".local/private.md", private_adapter):
                    self.assertIn((path, 1, "禁止发布的本地文件或产物"), issues)

    def test_static_config_is_checked_in_index_not_only_worktree(self):
        secret = "synthetic-staged-password"
        self.write(".env.example", "GAMEHUB_HUBDB_PASSWORD=" + secret + "\n")
        self.git("add", ".env.example")
        self.write(".env.example", "GAMEHUB_HUBDB_PASSWORD=${GAMEHUB_HUBDB_PASSWORD}\n")
        self.assertIn((".env.example", 1, "配置中含静态凭据"), CHECK.check_repository(self.root))
        self.assertEqual(CHECK.check_repository(self.root, all_files=True), [])
        self.git("add", ".env.example")
        self.write(".env.example", "GAMEHUB_HUBDB_PASSWORD=" + secret + "\n")
        self.assertEqual(CHECK.check_repository(self.root), [])
        self.assertIn((".env.example", 1, "配置中含静态凭据"), CHECK.check_repository(self.root, all_files=True))

    def test_private_paths_ignored_normally_and_force_add_rejected(self):
        """沿用真实忽略规则验证私有产物，防止 git add -f 绕过路径检查。"""
        self.write(".gitignore", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        private = ("config/runtime.local.json", "CMakeUserPresets.json", ".aws/credentials", "config/credentials.json", ".git-credentials",
                   "backups/gamehub.sql", "exports/players.csv", "data/raw/2026/players.json", "settings.json.bak")
        public = ("migrations/001_initial.sql", "migrations/003_backup_table.sql", "examples/players.sample.csv",
                  "examples/players-export.sample.csv", "config/credentials.example.json", ".env.example")
        for path in private + public:
            self.write(path, "{}\n" if path.endswith(".json") else "# synthetic\n")
        self.git("add", ".")
        staged = CHECK.git_paths(self.root, "ls-files", "--cached", "-z")
        self.assertTrue(set(private).isdisjoint(staged))
        self.assertTrue(set(public).issubset(staged))
        self.assertEqual(CHECK.check_repository(self.root, all_files=True), [])
        self.git("add", "--force", *private)
        for all_files in (False, True):
            issues = CHECK.check_repository(self.root, all_files=all_files)
            rejected = {path for path, _line, category in issues if category == "禁止发布的本地文件或产物"}
            self.assertEqual(rejected, set(private))

    def test_private_tree_is_ignored_but_public_docs_are_publishable(self):
        """普通添加只纳入公共文档，强制加入私有源码、资料和配置仍会被拒绝。"""
        self.write(".gitignore", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        private = {"private/adapters/SyntheticAdapter.cpp", "private/docs/notes.md", "private/config/runtime.json"}
        for path in private:
            self.write(path, "synthetic notes\n")
        self.write("docs/README.md", "synthetic public guide\n")
        self.write("README.md", "[guide](docs/README.md)\n")
        self.git("add", ".")
        staged = CHECK.git_paths(self.root, "ls-files", "--cached", "-z")
        self.assertTrue(private.isdisjoint(staged))
        self.assertIn("docs/README.md", staged)
        self.assertEqual(CHECK.check_repository(self.root), [])
        self.assertEqual(CHECK.check_repository(self.root, all_files=True), [])
        self.write("README.md", "[private](private/docs/notes.md)\n[encoded](%70rivate/docs/notes.md)\n")
        self.git("add", "README.md")
        for all_files in (False, True):
            issues = CHECK.check_repository(self.root, all_files=all_files)
            for line in (1, 2):
                self.assertIn(("README.md", line, "本地链接目标未纳入发布文件"), issues)
        self.git("add", "--force", *sorted(private))
        for all_files in (False, True):
            with self.subTest(all_files=all_files):
                issues = CHECK.check_repository(self.root, all_files=all_files)
                rejected = {path for path, _line, category in issues if category == "禁止发布的本地文件或产物"}
                self.assertEqual(rejected, private)
                for line in (1, 2):
                    self.assertIn(("README.md", line, "本地链接目标未纳入发布文件"), issues)

    def test_git_symlink_to_private_is_rejected_even_with_plain_worktree_file(self):
        """用真实 Git 链接模式验证强制暂存，兼容无创建符号链接权限的 Windows。"""
        self.write("private/adapters/SyntheticAdapter.cpp", "// synthetic private implementation\n")
        self.write("adapter-link.cpp", "private/adapters/SyntheticAdapter.cpp")
        blob = subprocess.run(["git", "-C", str(self.root), "hash-object", "-w", "--stdin"],
            input=b"private/adapters/SyntheticAdapter.cpp", capture_output=True, check=True).stdout.strip().decode()
        self.git("update-index", "--add", "--cacheinfo", "120000", blob, "adapter-link.cpp")
        self.write("README.md", "[adapter](adapter-link.cpp)\n")
        self.git("add", "README.md")
        self.assertEqual(CHECK.git_modes(self.root)["adapter-link.cpp"], b"120000")
        for all_files in (False, True):
            with self.subTest(all_files=all_files):
                issues = CHECK.check_repository(self.root, all_files=all_files)
                self.assertIn(("adapter-link.cpp", 1, "发布文件不能是本地符号链接"), issues)
                self.assertIn(("README.md", 1, "本地链接目标未纳入发布文件"), issues)


if __name__ == "__main__":
    unittest.main()

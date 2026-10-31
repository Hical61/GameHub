"""通过合成数据和临时仓库验证历史发布检查，不修改项目引用和公共 Git 配置。"""

from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-history.py"
SPEC = importlib.util.spec_from_file_location("history_check", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
PUBLISH_NAME = "hical"
PUBLISH_EMAIL = "Hical61@users.noreply.github.com"


class HistoryChecks(unittest.TestCase):
    """每项测试使用独立仓库，检查被删除文件和非分支引用也不会被漏扫。"""

    def setUp(self):
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        # 继承工作区 ACL，避免 Windows 受限令牌无法访问 Python 私有临时目录。
        self.root = (local / ("history-check-" + uuid.uuid4().hex)).resolve()
        self.assertEqual(self.root.parent, local.resolve())
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "user.name", PUBLISH_NAME)
        self.git("config", "user.email", PUBLISH_EMAIL)

    def cleanup(self):
        """只清理本测试创建的目录，递归删除前验证绝对路径范围。"""
        self.assertEqual(self.root.parent, (ROOT / ".local").resolve())
        def remove_readonly(function, path, _error):
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(self.root, onerror=remove_readonly)

    def git(self, *arguments, input=None, environment=None):
        """执行临时仓库操作并返回字节输出；测试数据不会进入命令行诊断。"""
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments], input=input, env=environment,
            capture_output=True, check=True,
        ).stdout

    def write(self, path, text):
        """创建 UTF-8/LF 测试文件，返回目标路径。"""
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
        return target

    def commit(self, message="synthetic change", environment=None):
        """提交临时文件并返回完整对象 ID；身份配置只影响本临时仓库。"""
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "--file=-", input=(message + "\n").encode(), environment=environment)
        return self.git("rev-parse", "HEAD").strip()

    def run_checker(self, *arguments):
        """运行真实 CLI 并返回合并输出，供退出码和输出脱敏断言使用。"""
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--repo", str(self.root), *arguments], capture_output=True,
        )
        return result.returncode, (result.stdout + result.stderr).decode("utf-8", errors="replace")

    def test_removed_docs_are_still_rejected(self):
        self.write("docs/private.md", "synthetic notes\n")
        self.commit()
        self.git("rm", "docs/private.md")
        self.write("README.md", "public\n")
        self.commit()
        issues = CHECK.check_repository(self.root)
        self.assertIn(("docs/private.md", 1, "禁止发布的本地文件或产物"), {item[1:] for item in issues})
        self.assertEqual(self.run_checker()[0], 1)

    def test_orphan_tree_reference_is_checked_and_explicit_scope_is_honored(self):
        self.write("README.md", "public\n")
        self.commit()
        branch = self.git("symbolic-ref", "HEAD").decode().strip()
        blob = self.git("hash-object", "-w", "--stdin", input=b"synthetic notes\n").strip()
        child = self.git("mktree", input=b"100644 blob " + blob + b"\tprivate.md\n").strip()
        tree = self.git("mktree", input=b"040000 tree " + child + b"\tdocs\n").strip()
        self.git("update-ref", "refs/private/snapshot", tree.decode())
        self.assertTrue(CHECK.check_repository(self.root))
        self.assertEqual(CHECK.check_repository(self.root, [branch]), [])
        self.assertEqual(self.run_checker()[0], 1)
        self.assertEqual(self.run_checker("--refs", branch)[0], 0)

    def test_orphan_blob_reference_is_rejected_without_path_context(self):
        self.write("README.md", "public\n")
        self.commit()
        secret = "synthetic-only-blob-password"
        blob = self.git("hash-object", "-w", "--stdin", input=('{"MYSQL_PASSWORD":"' + secret + '"}\n').encode()).strip()
        self.git("update-ref", "refs/private/config-snapshot", blob.decode())
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("独立文件引用缺少发布路径上下文", output)
        self.assertNotIn(secret, output)

    def test_secret_content_is_rejected_without_echo(self):
        secret = "ghp_" + "S" * 36
        self.write("settings.txt", secret + "\n")
        self.commit()
        self.write("settings.txt", "safe\n")
        self.commit()
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("疑似 GitHub 令牌", output)
        self.assertNotIn(secret, output)
        self.assertNotIn(str(self.root), output)

    def test_plaintext_configuration_and_runtime_artifacts_are_rejected(self):
        secret = "synthetic-only-password"
        self.write("example.json", '{"password":"' + secret + '"}\n')
        self.write("config/gamehub.json", "{}\n")
        self.write("output/server.exe", "synthetic artifact\n")
        self.commit()
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("配置中含静态凭据", output)
        self.assertIn("config/gamehub.json", output)
        self.assertIn("output/server.exe", output)
        self.assertNotIn(secret, output)

    def test_author_and_committer_emails_are_rejected_without_echo(self):
        self.write("README.md", "public\n")
        environment = os.environ.copy()
        environment.update(
            GIT_AUTHOR_NAME="Synthetic Private Author", GIT_AUTHOR_EMAIL="synthetic-author@example.invalid",
            GIT_COMMITTER_NAME="Synthetic Private Committer", GIT_COMMITTER_EMAIL="synthetic-committer@example.invalid",
        )
        self.commit(environment=environment)
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("作者邮箱不是 GitHub noreply", output)
        self.assertIn("提交者邮箱不是 GitHub noreply", output)
        for name in ("Synthetic Private Author", "Synthetic Private Committer", "synthetic-author@example.invalid", "synthetic-committer@example.invalid"):
            self.assertNotIn(name, output)

    def test_commit_message_secret_is_rejected_without_echo(self):
        secret = "sk-" + "proj-" + "M" * 40
        self.write("README.md", "public\n")
        self.commit("synthetic message\n\n" + secret)
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("@commit-message.txt", output)
        self.assertIn("疑似 OpenAI 密钥", output)
        self.assertNotIn(secret, output)

    def test_metadata_secret_is_rejected_without_echo(self):
        secret = "ghp_" + "H" * 36
        self.write("README.md", "public\n")
        environment = os.environ.copy()
        environment.update(GIT_AUTHOR_NAME="Synthetic " + secret)
        self.commit(environment=environment)
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("@metadata.txt", output)
        self.assertIn("疑似 GitHub 令牌", output)
        self.assertNotIn(secret, output)

    def test_commit_message_identity_trailer_is_rejected_without_echo(self):
        self.write("README.md", "public\n")
        identity = "Synthetic Contributor <synthetic-contributor@example.invalid>"
        self.commit("synthetic message\n\nCo-authored-by: " + identity)
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("说明署名邮箱不是 GitHub noreply", output)
        self.assertNotIn(identity, output)
        self.assertNotIn("synthetic-contributor@example.invalid", output)

    def test_ai_attribution_is_rejected_even_with_noreply_email(self):
        email = b"123456+synthetic-bot@users.noreply.github.com"
        for name in (b"Codex", b"OpenAI", b"ChatGPT", b"Claude", b"Copilot", b"Gemini", b"OpenAI Codex", b"Claude Code", b"GitHub Copilot",
                     b"Codex CLI", b"Claude Sonnet 4.5", b"GPT-5.4", b"codex-cli", b"Claude Sonnet4.5"):
            with self.subTest(name=name):
                message = b"synthetic message\n\nCo-authored-by: " + name + b" <" + email + b">\n"
                self.assertIn(("@commit-message.txt", 3, "禁止 AI 署名"), CHECK.message_issues("@commit-message.txt", message))
        self.write("README.md", "public\n")
        self.commit("synthetic message\n\nCo-authored-by: Codex <" + email.decode() + ">")
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("禁止 AI 署名", output)
        self.assertNotIn("Codex", output)
        self.assertNotIn(email.decode(), output)

    def test_human_trailer_and_ordinary_ai_examples_are_allowed(self):
        human = b"synthetic message\n\nCo-authored-by: Claude Martin <123456+synthetic-user@users.noreply.github.com>\n"
        example = b"synthetic example\n\n```text\nCo-authored-by: Codex <123456+synthetic-bot@users.noreply.github.com>\n```\n"
        for message in (human, example):
            self.assertEqual(CHECK.message_issues("@commit-message.txt", message), [])
        self.write("README.md", "Codex and Claude are ordinary documentation examples.\n")
        self.commit(human.decode().rstrip("\n"))
        self.assertEqual(CHECK.check_repository(self.root), [])

    def test_generated_attribution_rejects_markdown_and_emoji_variants(self):
        lines = (
            "Generated with Claude Code", "Generated by Codex CLI.",
            "🤖 Generated with [Claude Code](https://example.invalid/tool)",
            "**🤖 Generated with [Codex](https://example.invalid/tool)**",
            "[Generated with Claude Code](https://example.invalid/tool)",
            "🤖 **Generated by** [GitHub Copilot](https://example.invalid/tool)",
        )
        for line in lines:
            with self.subTest(line=line):
                message = ("synthetic message\n\n" + line + "\n").encode()
                self.assertIn(("@commit-message.txt", 3, "禁止 AI 署名"), CHECK.message_issues("@commit-message.txt", message))
        self.write("README.md", "public\n")
        self.commit("synthetic message\n\n" + lines[2])
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("禁止 AI 署名", output)
        self.assertNotIn("Claude Code", output)
        self.assertNotIn("example.invalid", output)

    def test_nonterminal_ai_identity_is_rejected(self):
        self.write("README.md", "public\n")
        self.commit("synthetic message\n\nCo-authored-by: Codex <bot@users.noreply.github.com>\n\nAdditional ordinary details.")
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn('"@commit-message.txt":3: 禁止 AI 署名', output)
        self.assertNotIn("Codex", output)
        self.assertNotIn("bot@users.noreply.github.com", output)

    def test_attribution_examples_and_ordinary_discussion_are_allowed(self):
        examples = (
            "```text\nGenerated with Claude Code\nCo-authored-by: Codex <bot@users.noreply.github.com>\n```\n",
            "~~~text\n🤖 Generated by [Claude Code](https://example.invalid/tool)\n~~~\n",
            "> Generated with Claude Code\n> Co-authored-by: Codex <bot@users.noreply.github.com>\n",
            "    Generated with Claude Code\n\tCo-authored-by: Codex <bot@users.noreply.github.com>\n",
            "`Generated with Claude Code`\n",
            "This change rejects Generated with Claude Code attribution.\n",
            "Generated with Claude Code examples are documented here.\n",
        )
        for example in examples:
            with self.subTest(example=example):
                self.assertEqual(CHECK.message_issues("@commit-message.txt", ("synthetic message\n\n" + example).encode()), [])

    def test_links_use_each_snapshot_and_exclude_forbidden_docs(self):
        self.write("README.md", "[guide](docs/guide.md)\n[future](future.md)\n")
        self.write("docs/guide.md", "synthetic guide\n")
        self.commit()
        self.write("future.md", "public\n")
        self.commit()
        issues = CHECK.check_repository(self.root)
        broken = {line for _object_id, path, line, category in issues if path == "README.md" and category == "本地链接目标未纳入发布文件"}
        self.assertEqual(broken, {1, 2})

    def test_annotated_tag_identity_and_message_are_checked(self):
        self.write("README.md", "public\n")
        commit = self.commit()
        secret = "ghp_" + "T" * 36
        tag = (b"object " + commit + b"\ntype commit\ntag synthetic\n"
               b"tagger Synthetic Tagger <synthetic-tagger@example.invalid> 1700000000 +0000\n\n"
               + secret.encode() + b"\n")
        object_id = self.git("hash-object", "-w", "-t", "tag", "--stdin", input=tag).strip()
        self.git("update-ref", "refs/tags/synthetic", object_id.decode())
        code, output = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIn("标签作者邮箱不是 GitHub noreply", output)
        self.assertIn("疑似 GitHub 令牌", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("synthetic-tagger@example.invalid", output)
        self.assertNotIn("Synthetic Tagger", output)

    def test_clean_history_with_noreply_identities_passes(self):
        self.write("README.md", "[license](LICENSE)\n")
        self.write("LICENSE", "synthetic license\n")
        self.commit()
        self.write("sample.txt", "mysql://reader:" + "${DATABASE_PASSWORD}@localhost/db\n")
        environment = os.environ.copy()
        environment.update(GIT_AUTHOR_EMAIL="123456+synthetic-user@users.noreply.github.com")
        self.commit(environment=environment)
        self.git("tag", "--annotate", "synthetic-clean", "--message", "synthetic tag")
        self.assertEqual(CHECK.check_repository(self.root), [])
        self.assertEqual(self.run_checker(), (0, ""))

    def test_detached_head_is_checked(self):
        self.write("README.md", "public\n")
        self.commit()
        self.git("checkout", "--detach", "--quiet")
        self.write("docs/detached.md", "synthetic detached notes\n")
        self.commit()
        self.assertTrue(CHECK.check_repository(self.root))

    def test_empty_repository_and_missing_selected_ref_have_defined_results(self):
        self.assertEqual(CHECK.check_repository(self.root), [])
        code, output = self.run_checker()
        self.assertEqual(code, 0)
        self.assertIn("没有可检查的本地引用", output)
        code, output = self.run_checker("--refs", "refs/heads/does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("无法完整读取 Git 历史", output)
        self.assertNotIn("does-not-exist", output)
        self.assertEqual(self.run_checker("--refs", "refs/heads/example^{tree}")[0], 1)

    def test_report_contains_only_short_object_id_and_issue_location(self):
        output = io.StringIO()
        with redirect_stdout(output):
            CHECK.report_issues([(b"a" * 40, "relative\nname.txt", 2, "合成问题")])
        self.assertEqual(output.getvalue(), 'aaaaaaaaaaaa "relative\\nname.txt":2: 合成问题\n')


if __name__ == "__main__":
    unittest.main()

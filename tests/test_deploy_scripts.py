"""Windows 部署脚本默认预览和目录边界回归，不创建或操作真实服务。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "需要 PowerShell 7")
class DeployTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ".local" / ("deploy-test-" + uuid.uuid4().hex)
        self.version = self.directory / "versions" / "synthetic-v1"
        (self.version / "bin").mkdir(parents=True)
        (self.version / "bin/gamehub.exe").write_bytes(b"synthetic-not-executable")
        self.wrapper = self.directory / "nssm.exe"
        self.wrapper.write_bytes(b"synthetic-not-executable")
        self.config = self.directory / "synthetic.json"
        self.config.write_text(json.dumps({"server": {"listen": "127.0.0.1", "port": 8443}}), encoding="utf-8")
        self.addCleanup(lambda: shutil.rmtree(self.directory))

    def command(self, version=None):
        """构造默认预览命令，绝不添加 Execute。"""
        return ["pwsh", "-NoProfile", "-File", str(ROOT / "deploy/gamehub-service.ps1"),
                "-DeploymentRoot", str(self.directory), "-VersionDirectory", str(version or self.version),
                "-ConfigPath", str(self.config), "-NssmPath", str(self.wrapper)]

    def test_default_dry_run_never_executes_binary_or_wrapper(self):
        result = subprocess.run(self.command(), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual((self.version / "bin/gamehub.exe").read_bytes(), b"synthetic-not-executable")

    def test_version_outside_deployment_root_is_rejected(self):
        result = subprocess.run(self.command(ROOT), capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

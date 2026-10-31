"""在临时安装树中验证许可缺失、篡改及编译器运行库的检查。"""

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_install", ROOT / "scripts/check-install.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class InstalledLicenseChecks(unittest.TestCase):
    """仅使用合成许可，不读取真实玩家数据或已有安装目录。"""

    def setUp(self):
        local = ROOT / ".local"
        local.mkdir(exist_ok=True)
        self.prefix = (local / ("license-test-" + uuid.uuid4().hex)).resolve()
        self.prefix.mkdir()
        self.addCleanup(self.remove_temporary_tree)
        (self.prefix / "bin").mkdir()
        self.licenses = self.prefix / "share/gamehub/licenses"
        self.records = []
        for component in sorted(CHECK.REQUIRED_LICENSE_COMPONENTS):
            self.add_license(component, "LICENSE")
        self.add_license("gamehub", "THIRD_PARTY_NOTICES.md")
        self.write_manifest()

    def remove_temporary_tree(self):
        """只删除本测试创建的工作区内目录，无返回值。"""
        if self.prefix.parent != (ROOT / ".local").resolve() or not self.prefix.name.startswith("license-test-"):
            raise RuntimeError("测试临时目录超出清理范围")
        shutil.rmtree(self.prefix)

    def add_license(self, component, name):
        """添加合成许可，返回安装后的绝对路径。"""
        relative = f"{component}/{len(self.records) + 1}-{name}"
        path = self.licenses / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (component + " synthetic license\n").encode()
        path.write_bytes(content)
        self.records.append((component, relative, hashlib.sha256(content).hexdigest()))
        return path

    def write_manifest(self):
        """将当前合成记录写入清单，无返回值。"""
        (self.licenses / "manifest.tsv").write_text(
            "component\tfile\tsha256\n" + "".join("\t".join(record) + "\n" for record in self.records),
            encoding="utf-8")

    def test_complete_installation(self):
        CHECK.check_licenses(self.prefix)

    def test_missing_component_even_when_manifest_omits_it(self):
        self.records = [record for record in self.records if record[0] != "openssl"]
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "缺少组件许可：openssl"):
            CHECK.check_licenses(self.prefix)

    def test_missing_or_empty_or_modified_license(self):
        path = self.licenses / self.records[0][1]
        for content in (b"", b" \n", b"changed"):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(RuntimeError):
                    CHECK.check_licenses(self.prefix)
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, "缺少许可"):
            CHECK.check_licenses(self.prefix)

    def test_missing_notice_and_manifest(self):
        self.records = [record for record in self.records if not record[1].endswith("THIRD_PARTY_NOTICES.md")]
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "缺少 GameHub 声明"):
            CHECK.check_licenses(self.prefix)
        (self.licenses / "manifest.tsv").unlink()
        with self.assertRaisesRegex(RuntimeError, "缺少许可清单"):
            CHECK.check_licenses(self.prefix)

    def test_runtime_dll_requires_its_license(self):
        for binary, components in (("vcruntime140.dll", ("msvc-runtime",)),
                                   ("libstdc++-6.dll", ("gcc-runtime", "mingw-runtime")),
                                   ("libwinpthread-1.dll", ("winpthreads",))):
            with self.subTest(binary=binary):
                path = self.prefix / "bin" / binary
                path.touch()
                with self.assertRaisesRegex(RuntimeError, "缺少组件许可"):
                    CHECK.check_licenses(self.prefix)
                for component in components:
                    self.add_license(component, "LICENSE")
                self.write_manifest()
                CHECK.check_licenses(self.prefix)
                path.unlink()

    def test_manifest_rejects_escaping_paths(self):
        self.records.append(("boost", "boost/../../outside", "0" * 64))
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "无效记录"):
            CHECK.check_licenses(self.prefix)

    def test_cmake_rejects_empty_source_license(self):
        source = self.prefix / "empty"
        source.touch()
        script = self.prefix / "check.cmake"
        script.write_text(f'include("{(ROOT / "cmake/InstallLicenses.cmake").as_posix()}")\n'
                          f'gamehub_install_component_licenses(example "{source.as_posix()}")\n', encoding="utf-8")
        result = subprocess.run(["cmake", "-P", str(script)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("许可文件为空".encode(), result.stderr)

    def test_cmake_package_manager_layouts(self):
        for layout in ("vcpkg", "msys2"):
            with self.subTest(layout=layout):
                source = self.prefix / layout
                source.mkdir()
                dependency = source / "dependency"
                (dependency / "include").mkdir(parents=True)
                (dependency / "bin").mkdir()
                compiler = dependency / "bin" / "g++.exe"
                compiler.touch()
                packages = ("boost-asio", "openssl", "zlib") if layout == "vcpkg" else (
                    "boost", "boost-libs", "openssl", "zlib", "gcc-libs", "crt", "winpthreads")
                for package in packages:
                    name = f"share/{package}/copyright" if layout == "vcpkg" else f"share/licenses/{package}/LICENSE"
                    path = dependency / name
                    path.parent.mkdir(parents=True)
                    path.write_text(package + " synthetic license\n", encoding="utf-8")
                hical = source / "hical"
                pico = hical / "src/third_party/picohttpparser"
                pico.mkdir(parents=True)
                (hical / "LICENSE").write_text("synthetic hical license\n", encoding="utf-8")
                for name in ("picohttpparser.c", "picohttpparser.h"):
                    (pico / name).write_text("/* Copyright synthetic\nPermission is hereby granted synthetic */\n", encoding="utf-8")
                for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
                    (source / name).write_text("synthetic project notice\n", encoding="utf-8")
                hicalOverride = 'set(GAMEHUB_LICENSE_HICAL_FILES "hical/LICENSE")\n' if layout == "msys2" else ''
                cmake = (f'cmake_minimum_required(VERSION 3.28)\nproject(LicenseFixture LANGUAGES NONE)\n'
                         f'include("{(ROOT / "cmake/InstallLicenses.cmake").as_posix()}")\n'
                         f'set(GAMEHUB_HICAL_SOURCE "{hical.as_posix()}")\n'
                         f'set(gamehubHicalSource "{hical.as_posix()}")\n'
                         f'set(Boost_INCLUDE_DIR "{dependency.as_posix()}/include")\n'
                         f'set(OPENSSL_INCLUDE_DIR "{dependency.as_posix()}/include")\n'
                         f'set(ZLIB_INCLUDE_DIR "{dependency.as_posix()}/include")\n'
                         f'set(CMAKE_CXX_COMPILER "{compiler.as_posix()}")\n'
                         f'set(MINGW {"TRUE" if layout == "msys2" else "FALSE"})\n'
                         f'{hicalOverride}'
                         f'gamehub_install_licenses()\n')
                (source / "CMakeLists.txt").write_text(cmake, encoding="utf-8")
                build = source / "build"
                installed = source / "installed"
                for command in (["cmake", "-S", str(source), "-B", str(build)],
                                ["cmake", "--install", str(build), "--prefix", str(installed)]):
                    result = subprocess.run(command, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))
                CHECK.check_licenses(installed)
                if layout == "msys2":
                    manifest = (installed / "share/gamehub/licenses/manifest.tsv").read_text(encoding="utf-8")
                    for component in ("gcc-runtime", "mingw-runtime", "winpthreads"):
                        self.assertIn(component + "\t", manifest)
                licensePath = hical / "LICENSE"
                original = licensePath.read_bytes()
                originalStat = licensePath.stat()
                installCommand = ["cmake", "--install", str(build), "--prefix", str(installed)]
                result = subprocess.run(installCommand, capture_output=True, cwd=build)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                # CMake 会按时间戳跳过复制；保留原时间，确保检查实际比较文件内容。
                for content in (b"", b" " * len(original), b"x" * len(original)):
                    with self.subTest(source_content=content):
                        licensePath.write_bytes(content)
                        os.utime(licensePath, ns=(originalStat.st_atime_ns, originalStat.st_mtime_ns))
                        result = subprocess.run(installCommand, capture_output=True, cwd=build)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("原始许可与配置时的文件不一致".encode(), result.stderr)
                licensePath.write_bytes(original)
                os.utime(licensePath, ns=(originalStat.st_atime_ns, originalStat.st_mtime_ns))
                result = subprocess.run(installCommand, capture_output=True, cwd=build)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                licensePath.unlink()
                result = subprocess.run(installCommand, capture_output=True, cwd=build)
                self.assertNotEqual(result.returncode, 0)

    def test_missing_dependency_does_not_use_parent_project_license(self):
        script = self.prefix / "check.cmake"
        (self.prefix / "LICENSE").write_text("synthetic project license\n", encoding="utf-8")
        script.write_text(f'cmake_minimum_required(VERSION 3.28)\n'
                          f'include("{(ROOT / "cmake/InstallLicenses.cmake").as_posix()}")\n'
                          f'gamehub_find_package_licenses(found ROOTS "{self.prefix.as_posix()}" PACKAGES openssl)\n'
                          'gamehub_install_component_licenses(openssl ${found})\n', encoding="utf-8")
        result = subprocess.run(["cmake", "-P", str(script)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少 openssl 的许可文件".encode(), result.stderr)


if __name__ == "__main__":
    unittest.main()

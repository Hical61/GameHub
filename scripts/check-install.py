"""安装到临时目录，再用干净的 PATH 检查程序能否启动。"""

import argparse
import csv
import hashlib
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_LICENSE_COMPONENTS = frozenset(("gamehub", "hical", "boost", "openssl", "zlib", "picohttpparser"))


def check_licenses(prefix):
    """核对安装清单、全部许可正文和摘要；失败抛出 RuntimeError，无返回值。"""
    license_root = prefix / "share/gamehub/licenses"
    manifest = license_root / "manifest.tsv"
    if not manifest.is_file():
        raise RuntimeError("安装目录缺少许可清单：share/gamehub/licenses/manifest.tsv")
    components = set()
    paths = set()
    with manifest.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != ["component", "file", "sha256"]:
            raise RuntimeError("许可清单格式不正确")
        for record in reader:
            component, relative, expected = (record.get(key) or "" for key in reader.fieldnames)
            path = PurePosixPath(relative)
            if (None in record or not re.fullmatch(r"[a-z][a-z0-9-]*", component)
                    or "\\" in relative or path.is_absolute() or ".." in path.parts
                    or len(path.parts) < 2 or path.parts[0] != component
                    or not re.fullmatch(r"[0-9a-f]{64}", expected) or relative in paths):
                raise RuntimeError("许可清单包含无效记录")
            installed = license_root.joinpath(*path.parts)
            if not installed.is_file() or not installed.resolve().is_relative_to(license_root.resolve()):
                raise RuntimeError("安装目录缺少许可或路径超出范围：" + relative)
            content = installed.read_bytes()
            if not content.strip():
                raise RuntimeError("安装许可为空：" + relative)
            if hashlib.sha256(content).hexdigest() != expected:
                raise RuntimeError("安装许可摘要不匹配：" + relative)
            components.add(component)
            paths.add(relative)
    required = set(REQUIRED_LICENSE_COMPONENTS)
    binaries = {path.name.lower() for path in (prefix / "bin").glob("*.dll")}
    if any(name.startswith(("msvcp", "vcruntime", "concrt")) for name in binaries):
        required.add("msvc-runtime")
    if any(name.startswith(("libgcc_s", "libstdc++")) for name in binaries):
        required.update(("gcc-runtime", "mingw-runtime"))
    if "libwinpthread-1.dll" in binaries:
        required.add("winpthreads")
    missing = required - components
    if missing:
        raise RuntimeError("安装目录缺少组件许可：" + ", ".join(sorted(missing)))
    # GameHub 的 MIT 和第三方说明必须分别保留，不能用清单中的单个组件名替代。
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        if not any(PurePosixPath(path).name.endswith("-" + name) for path in paths if path.startswith("gamehub/")):
            raise RuntimeError("安装目录缺少 GameHub 声明：" + name)


def check_install(build_dir, config):
    """安装指定构建并运行 --version；成功删除临时目录，失败保留安装文件供排查。"""
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    prefix = (local / ("install-check-" + uuid.uuid4().hex)).resolve()
    prefix.mkdir()
    succeeded = False
    try:
        subprocess.run(["cmake", "--install", str(build_dir.resolve()), "--config", config,
                        "--prefix", str(prefix)], check=True)
        for relative in ("config/gamehub.example.json", "share/gamehub/migrations/001_initial.sql",
                         "share/gamehub/migrations/002_operations.sql"):
            if not (prefix / relative).is_file():
                raise RuntimeError("安装目录缺少文件：" + relative)
        check_licenses(prefix)
        environment = dict(os.environ)
        if os.name == "nt":
            windows = Path(os.environ["SystemRoot"])
            environment["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
        else:
            environment["PATH"] = os.defpath
        binary = prefix / "bin" / ("gamehub.exe" if os.name == "nt" else "gamehub")
        result = subprocess.run([str(binary), "--version"], cwd=prefix / "bin", env=environment,
                                capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(f"安装后的程序启动失败，退出码 0x{result.returncode & 0xffffffff:08X}")
        succeeded = True
        print("安装检查通过：许可清单及正文齐备，程序在干净 PATH 下启动成功")
    finally:
        if succeeded:
            if prefix.parent != local.resolve() or not prefix.name.startswith("install-check-"):
                raise RuntimeError("临时安装目录超出清理范围")
            shutil.rmtree(prefix)
        else:
            print("安装文件已保留：" + str(prefix), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True, help="已经构建完成的 CMake 目录")
    parser.add_argument("--config", default="RelWithDebInfo", help="构建配置，默认 RelWithDebInfo")
    args = parser.parse_args()
    try:
        check_install(args.build_dir, args.config)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("安装检查失败：" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

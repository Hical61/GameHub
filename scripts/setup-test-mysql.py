"""下载并校验固定的 MySQL 测试工具，只解压到项目的本地目录。"""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MYSQL_VERSION = "8.0.16"
ARCHIVE_NAME = "mysql-8.0.16-winx64.zip"
ARCHIVE_ROOT = "mysql-8.0.16-winx64"
ARCHIVE_URL = "https://cdn.mysql.com/archives/mysql-8.0/" + ARCHIVE_NAME
# 已与同一官方归档的 .md5 核对，再固定 SHA-256，运行时不信任动态下载的校验文件。
ARCHIVE_SHA256 = "51ab649e14570498a5f23b81578ed0e464d8e48fda83f097ea61087262d2e35e"
ARCHIVE_BYTES = 240017371
DOWNLOAD_SECONDS = 480
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
RENAME_ATTEMPTS = 6
RENAME_PAUSE_SECONDS = 0.05


def sha256(path):
    """返回文件的 SHA-256，分块读取，避免把整个 ZIP 放进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_archive(destination):
    """返回校验通过的 ZIP；缓存损坏时直接报错，网络失败最多重试三次。"""
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / ARCHIVE_NAME
    if archive.exists():
        if archive.stat().st_size != ARCHIVE_BYTES or sha256(archive) != ARCHIVE_SHA256:
            raise ValueError("已有 MySQL ZIP 的 SHA-256 不符，请检查后删除该缓存")
        return archive
    partial = archive.with_suffix(".zip.part")
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for attempt in range(3):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MySQL 下载超过总时限")
            with urllib.request.urlopen(ARCHIVE_URL, timeout=min(60, remaining)) as response, partial.open("wb") as output:
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) != ARCHIVE_BYTES:
                    raise ValueError("MySQL ZIP 声明长度与固定包不符")
                received = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("MySQL 下载超过总时限")
                    # urllib 的 HTTPResponse 保留底层 socket，逐次更新剩余等待预算。
                    # read1 最多做一次底层读取，不能用 read(n) 等一整块慢慢拼满。
                    if response.fp is not None:
                        response.fp.raw._sock.settimeout(min(60, remaining))
                    block = response.read1(min(DOWNLOAD_CHUNK_BYTES, ARCHIVE_BYTES - received + 1))
                    if not block:
                        break
                    if len(block) > ARCHIVE_BYTES - received:
                        raise ValueError("MySQL ZIP 超出固定字节长度")
                    output.write(block)
                    received += len(block)
                if received != ARCHIVE_BYTES:
                    raise ValueError("MySQL ZIP 未完整下载")
            if sha256(partial) != ARCHIVE_SHA256:
                raise ValueError("下载的 MySQL ZIP 校验失败")
            partial.replace(archive)
            return archive
        except (OSError, urllib.error.URLError) as error:
            if attempt == 2 or time.monotonic() >= deadline:
                raise RuntimeError("MySQL 下载失败：" + type(error).__name__) from None
            print("MySQL 下载中断，稍后重试", file=sys.stderr)
            time.sleep(min(attempt + 1, deadline - time.monotonic()))
        finally:
            partial.unlink(missing_ok=True)


def rename_directory(source, target):
    """不覆盖目标；Windows 文件短暂占用时最多尝试六次，总退让不超过 0.75 秒。"""
    for attempt in range(RENAME_ATTEMPTS):
        if target.exists():
            raise FileExistsError("MySQL 安装目录已被创建，未覆盖已有内容")
        try:
            source.rename(target)
            return
        except PermissionError as error:
            if (sys.platform != "win32" or getattr(error, "winerror", None) not in (5, 32, 33)
                    or attempt == RENAME_ATTEMPTS - 1):
                raise
            time.sleep(RENAME_PAUSE_SECONDS * (attempt + 1))


def unpack_archive(archive, destination):
    """解压已校验的归档并返回 bin 目录；不覆盖已有但来源不明的安装目录。"""
    if sha256(archive) != ARCHIVE_SHA256:
        raise ValueError("MySQL ZIP 校验失败，未解压任何文件")
    target = destination / ARCHIVE_ROOT
    marker = target / "gamehub-source.sha256"
    if target.exists():
        if marker.is_file() and marker.read_text(encoding="ascii").strip() == ARCHIVE_SHA256:
            return target / "bin"
        raise ValueError("MySQL 解压目录已经存在但校验记录不匹配，请先检查该目录")
    staging = destination / ("unpack-" + uuid.uuid4().hex)
    staging.mkdir()
    failed = False
    try:
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                path = PurePosixPath(item.filename)
                if (not path.parts or path.parts[0] != ARCHIVE_ROOT or ".." in path.parts
                        or "\\" in item.filename or ":" in item.filename or path.is_absolute()):
                    raise ValueError("MySQL ZIP 包含预期目录之外的文件")
            source.extractall(staging)
        unpacked = staging / ARCHIVE_ROOT
        if not all((unpacked / "bin" / name).is_file() for name in ("mysql.exe", "mysqld.exe")):
            raise ValueError("MySQL ZIP 缺少测试需要的程序")
        (unpacked / marker.name).write_text(ARCHIVE_SHA256 + "\n", encoding="ascii", newline="\n")
        rename_directory(unpacked, target)
    except BaseException:
        failed = True
        raise
    finally:
        try:
            resolved = staging.resolve()
            if resolved.parent != destination.resolve() or not resolved.name.startswith("unpack-"):
                raise RuntimeError("临时解压目录超出清理范围")
            shutil.rmtree(resolved)
        except Exception:
            if not failed:
                raise
            print("临时解压目录未能清理，请检查：" + str(staging), file=sys.stderr)
    return target / "bin"


def prepare_mysql(destination):
    """下载和解压固定版本，确认程序版本后返回工具目录，不启动数据库。"""
    destination = destination.resolve()
    binary_directory = unpack_archive(download_archive(destination), destination)
    for name in ("mysql.exe", "mysqld.exe"):
        result = subprocess.run([str(binary_directory / name), "--no-defaults", "--version"],
                                capture_output=True, timeout=15, check=True)
        if MYSQL_VERSION.encode("ascii") not in result.stdout:
            raise ValueError("MySQL 工具报告的版本与固定版本不符")
    return binary_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / ".local/test-mysql",
                        help="ZIP 缓存和解压目录，默认位于项目 .local/test-mysql")
    parser.add_argument("--github-output", type=Path, help="GitHub Actions 的步骤输出文件")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("此脚本获取 Windows x64 测试工具，其他系统请直接指定已有 MySQL 工具目录")
    try:
        binary_directory = prepare_mysql(args.destination)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
                output.write("mysql-bin=" + str(binary_directory) + "\n")
        print("MySQL " + MYSQL_VERSION + " 工具目录：" + str(binary_directory))
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print("准备 MySQL 失败：" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

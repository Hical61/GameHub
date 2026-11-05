"""从当前 Git 工作树准备不含私有文件的公开源码副本，不构建、不联网、不操作暂存内容。"""

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ".public-source-manifest.json"
PUBLIC_DIRECTORIES = {".github", ".githooks", "cmake", "config", "deploy", "docs", "examples", "migrations",
                      "scripts", "src", "tests"}
PUBLIC_ROOT_FILES = {".clang-format", ".clang-tidy", ".editorconfig", ".gitattributes", ".gitignore",
                     ".env.example", ".env.sample", "CHANGELOG.md", "CMakeLists.txt", "CONTRIBUTING.md",
                     "LICENSE", "README.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "vcpkg.json"}
MAX_FILES = 10000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SPEC = importlib.util.spec_from_file_location("public_source_release_policy", ROOT / "scripts/check-release.py")
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class SourceError(ValueError):
    """只携带固定问题类别，不包含文件正文或底层异常原文。"""


def git_output(source, *arguments):
    """只读查询 Git 文件名或工作树根；返回原始字节，不查询任何 blob。"""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c",
                             "core.untrackedCache=false", "-C", str(source), *arguments],
                            capture_output=True, timeout=30, env=environment)
    if result.returncode:
        raise SourceError("无法读取 Git 公开候选文件名")
    return result.stdout


def no_links(path):
    """检查既有路径的每一级；符号链接、Windows junction 和其他 reparse point 都拒绝。"""
    absolute = Path(os.path.abspath(path))
    for item in reversed((absolute, *absolute.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
            raise SourceError("路径含符号链接或重解析点")
    return absolute


def public_path(relative):
    """验证仓库相对路径及公开根白名单，复用发布检查器的私有文件与产物规则。"""
    path = PurePosixPath(relative)
    if (not relative or "\\" in relative or path.is_absolute() or path.as_posix() != relative
            or any(ord(character) < 32 for character in relative)
            or any(part in {".", ".."} or ":" in part or part.endswith((" ", "."))
                   or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.IGNORECASE)
                   for part in path.parts)):
        return False
    # 大小写折叠也拒绝 private，避免跨平台导出后绕过边界。
    if any(part.casefold() == "private" for part in path.parts):
        return False
    return (not POLICY.forbidden_path(relative)
            and ((len(path.parts) == 1 and relative in PUBLIC_ROOT_FILES)
                 or (len(path.parts) > 1 and path.parts[0] in PUBLIC_DIRECTORIES)))


def snapshot_file(source, relative):
    """读取一个有界普通文件；读前读后检查完整路径及文件身份，绝不复制链接本身。"""
    path = no_links(source / relative)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES or before.st_nlink != 1:
        raise SourceError("候选不是有界普通文件")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise SourceError("候选不是单链接普通文件")
        data = handle.read(MAX_FILE_BYTES + 1)
        after_read = os.fstat(handle.fileno())
    no_links(path)
    after = path.lstat()
    fingerprints = {(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    for info in (before, opened, after_read, after)}
    if len(data) > MAX_FILE_BYTES or len(fingerprints) != 1 or len(data) != before.st_size:
        raise SourceError("读取期间候选文件发生变化")
    return data, stat.S_IMODE(before.st_mode)


def check_index_types(source):
    """只读 Git 模式元数据；拒绝链接、子模块及未合并项，不读取索引中的文件内容。"""
    for entry in git_output(source, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        fields = metadata.split()
        if (not separator or not path or len(fields) != 3 or fields[2] != b"0"
                or fields[0] not in {b"100644", b"100755"}):
            raise SourceError("Git 候选类型不是普通文件，或索引尚未合并")


def candidate_snapshot(source):
    """快照 tracked 与 unignored 文件的当前工作树字节；已在工作树删除的文件不导出。"""
    source = no_links(source)
    top = git_output(source, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if Path(top).resolve() != source:
        raise SourceError("source 必须是 Git 工作树根")
    names = git_output(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    try:
        paths = sorted({item.decode("utf-8") for item in names.split(b"\0") if item})
    except UnicodeError:
        raise SourceError("Git 候选文件名不是 UTF-8") from None
    if len(paths) > MAX_FILES:
        raise SourceError("公开候选数量超过上限")
    # core.symlinks=false 可把 mode=120000 检出为普通文本，不能只依赖工作树 lstat。
    check_index_types(source)
    snapshot = {}
    folded = set()
    total = 0
    for relative in paths:
        if not public_path(relative):
            raise SourceError("Git 候选混入非公开路径、凭据或生成文件")
        if relative.casefold() in folded:
            raise SourceError("公开候选存在跨平台大小写冲突")
        folded.add(relative.casefold())
        try:
            data, mode = snapshot_file(source, relative)
        except FileNotFoundError:
            # 只按当前工作树发布，不从 index/commit 恢复已删除文件。
            continue
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SourceError("公开候选总字节数超过上限")
        snapshot[relative] = (data, mode)
    if not {"CMakeLists.txt", "README.md"}.issubset(snapshot):
        raise SourceError("公开源码缺少根 CMakeLists.txt 或 README.md")
    return source, snapshot


def dependency_path(source, target):
    """规范化源码内的静态本地依赖；CMake 外部依赖变量交给实际独立构建验证。"""
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), target.replace("\\", "/")))
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        return None
    return normalized


def cmake_commands(text):
    """提取常见 CMake 命令及其参数；保留引号内括号，跳过行注释，不解释变量。"""
    index = 0
    while index < len(text):
        if text[index] == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end + 1
            continue
        match = re.match(r"([A-Za-z_]\w*)\s*\(", text[index:])
        if not match:
            index += 1
            continue
        start = index
        index += match.end()
        body = []
        depth = 1
        quoted = False
        while index < len(text) and depth:
            character = text[index]
            if character == "\\" and quoted and index + 1 < len(text):
                body.append(text[index:index + 2])
                index += 2
                continue
            if character == '"':
                quoted = not quoted
            if not quoted:
                if character == "#":
                    end = text.find("\n", index)
                    index = len(text) if end < 0 else end
                    body.append("\n")
                    continue
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
            if depth:
                body.append(character)
            index += 1
        tokens = [token[1:-1] if token.startswith('"') else token
                  for token in re.findall(r'"(?:\\.|[^"\\])*"|[^\s;()]+', "".join(body))]
        yield match[1].lower(), tokens, text.count("\n", 0, start) + 1


def cmake_issues(path, text, published):
    """检查可静态解析的 CMake 文件依赖；第三方路径和变量须再经独立构建验证。"""
    issues = []
    source_suffixes = (".cpp", ".c", ".h", ".hpp", ".cmake", ".py", ".json", ".sql", ".patch", ".in")
    for command, tokens, number in cmake_commands(text):
        if command in {"add_library", "add_executable", "target_sources"}:
            targets = [item for item in tokens[1:] if item.endswith(source_suffixes)]
        elif command == "include":
            targets = tokens[:1] if tokens and ("/" in tokens[0] or tokens[0].endswith(".cmake")) else []
        elif command in {"configure_file", "add_subdirectory"}:
            targets = tokens[:1]
        else:
            continue
        for token in targets:
            parts = token.replace("\\", "/").split("/")
            if any(part.casefold() in {"private", ".local"} for part in parts):
                issues.append((path, number, "CMake 依赖私有文件"))
                continue
            base = path
            if token.startswith("${CMAKE_CURRENT_SOURCE_DIR}/"):
                # include() 中的 CURRENT_SOURCE_DIR 仍是调用目录，不是 .cmake 所在目录。
                base = "CMakeLists.txt" if path.endswith(".cmake") else path
                token = token.removeprefix("${CMAKE_CURRENT_SOURCE_DIR}/")
            for prefix in ("${CMAKE_CURRENT_LIST_DIR}/", "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/"):
                token = token.removeprefix(prefix)
            if "$" in token or "<" in token:
                continue
            target = dependency_path(base, token)
            exists = target in published if target is not None else False
            if command == "add_subdirectory" and target is not None:
                exists = target + "/CMakeLists.txt" in published or (target == "." and "CMakeLists.txt" in published)
            if not exists:
                issues.append((path, number, "CMake 静态依赖未纳入公开源码"))
    return issues


def dependency_issues(path, text, published):
    """检查 CMake 静态源码、显式私有 include 和 Python 相对导入，不执行待检代码。"""
    issues = []
    suffix = PurePosixPath(path).suffix
    if suffix in {".h", ".hpp", ".c", ".cpp"}:
        for number, line in enumerate(text.splitlines(), 1):
            include = re.match(r'\s*#\s*include\s*["<]([^">]+)[">]', line)
            if include and any(part.casefold() in {"private", ".local"} for part in include[1].replace("\\", "/").split("/")):
                issues.append((path, number, "源码依赖私有文件"))
            elif include and '"' in line[:include.end()]:
                local = dependency_path(path, include[1])
                rooted = dependency_path("CMakeLists.txt", "src/" + include[1])
                if not ({local, rooted} & published):
                    issues.append((path, number, "本地 include 未纳入公开源码"))
    if PurePosixPath(path).name == "CMakeLists.txt" or suffix == ".cmake":
        issues.extend(cmake_issues(path, text, published))
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return issues  # 发布检查器会报告语法错误。
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                target = dependency_path(path, "../" * (node.level - 1) + node.module.replace(".", "/"))
                if target is None or not ({target + ".py", target + "/__init__.py"} & published):
                    issues.append((path, node.lineno, "Python 相对导入未纳入公开源码"))
    return issues


def check_snapshot(snapshot):
    """沿用公开发布内容检查，并验证静态依赖闭包；返回去重问题，不含原文。"""
    published = set(snapshot)
    issues = []
    for path, (data, _mode) in snapshot.items():
        issues.extend(POLICY.check_file(path, data, published))
        issues.extend(dependency_issues(path, data.decode("utf-8", errors="replace"), published))
    return sorted(set(issues))


def output_path(source, output):
    """只允许全新目录；源内输出限定于 .local/public-source-check 下，绝不覆盖或删除。"""
    path = no_links(output if output is not None else source / ".local/public-source-check" / uuid.uuid4().hex)
    if path.exists() or path == source or path in source.parents:
        raise SourceError("输出必须是全新的非祖先目录")
    if path.is_relative_to(source):
        base = source / ".local/public-source-check"
        if not path.is_relative_to(base) or path == base:
            raise SourceError("源内输出只能位于 .local/public-source-check 的新目录")
    return path


def prepare_source(source=ROOT, output=None):
    """预检后复制当前公开快照，返回摘要；失败不删除目录，也不留下成功 manifest。"""
    source, snapshot = candidate_snapshot(source)
    issues = check_snapshot(snapshot)
    if issues:
        POLICY.report_issues(issues)
        raise SourceError("公开源码内容或静态依赖检查失败")
    destination = output_path(source, output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    no_links(destination.parent)
    destination.mkdir()  # 全新目录，失败时保留现场；不使用递归删除。
    entries = []
    for relative, (data, mode) in snapshot.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        no_links(target.parent)
        with target.open("xb") as handle:
            handle.write(data)
        target.chmod(mode)
        entries.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"schemaVersion": 1, "status": "prepared", "selection": "git-tracked-and-unignored-worktree",
                "fileCount": len(entries), "totalBytes": sum(item["size"] for item in entries), "files": entries,
                "validation": {"releaseContent": "passed", "staticDependencies": "passed",
                               "independentBuild": "not-run", "tests": "not-run"}}
    with (destination / MANIFEST).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {"output": str(destination), "manifest": str(destination / MANIFEST), "fileCount": len(entries),
            "totalBytes": manifest["totalBytes"], "status": "prepared"}


def main():
    """命令行仅准备副本；构建与测试由调用方在返回目录中显式执行。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT, help="Git 工作树根，默认当前项目")
    parser.add_argument("--output", type=Path, help="全新目标目录；默认 .local/public-source-check/<UUID>")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_source(args.source, args.output), ensure_ascii=False))
        return 0
    except SourceError as error:
        # SourceError 只由本文件的固定类别构造，不包含输入正文或底层异常。
        print(f"公开源码副本准备失败：{error}。", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        # 底层异常可能含机器路径/原文；只给固定诊断，不输出异常对象。
        print("公开源码副本准备失败；未构建、未操作暂存内容。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

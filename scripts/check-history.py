"""只读检查全部本地引用的发布历史，只输出路径、行号、问题类型和对象短 ID。"""

import argparse
from collections import OrderedDict
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("history_release_check", ROOT / "scripts/check-release.py")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)
OBJECT_ID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")
NOREPLY_EMAIL = re.compile(
    rb"(?:[0-9]+\+)?[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?(?:\[bot\])?@users\.noreply\.github\.com",
    re.IGNORECASE,
)
SIGNATURE = re.compile(rb".* <([^<>]+)> -?[0-9]+ [+-][0-9]{4}")
IDENTITY_TRAILER = re.compile(
    rb"^(?:co-authored-by|signed-off-by|reviewed-by|acked-by|tested-by|reported-by|suggested-by):[ \t]*(.*)$",
    re.IGNORECASE,
)
TRAILER_SIGNATURE = re.compile(rb"(?P<name>.*) <(?P<email>[^<>]+)>[ \t]*")
GENERATION_ATTRIBUTION = re.compile(rb"generated[ \t]+(?:with|by)[ \t]+(?P<name>.+?)[.!]?", re.IGNORECASE)
AI_ATTRIBUTION_NAME = re.compile(
    rb"(?:codex(?:[- \t]+cli)?|openai(?:[- \t]+codex(?:[- \t]+cli)?)?|chatgpt|"
    rb"claude(?:[- \t]+(?:code|(?:haiku|sonnet|opus)(?:[- \t]*[0-9]+(?:\.[0-9]+)*)?))?|"
    rb"(?:openai[ \t]+)?gpt[- \t]?[0-9]+(?:\.[0-9]+)*(?:[- \t]+(?:codex|mini|nano|turbo))?|"
    rb"(?:github[ \t]+)?copilot(?:[ \t]+cli)?|(?:google[ \t]+)?gemini"
    rb"(?:[ \t]+(?:advanced|[0-9]+(?:\.[0-9]+)*(?:[ \t]+(?:pro|flash))?))?)"
    rb"(?:[ \t]*(?:\[bot\]|\(bot\)|bot|assistant|ai))?",
    re.IGNORECASE,
)
CACHE_BYTES = 16 * 1024 * 1024
SHORT_ID_LENGTH = 12


class HistoryReadError(ValueError):
    """历史读取失败；异常不携带 Git 原文、文件内容或身份信息。"""


def git_environment():
    """返回只读 Git 环境；禁用替换对象和缺失对象的自动联网下载。"""
    environment = os.environ.copy()
    environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")
    return environment


def git_result(root, *arguments):
    """在指定仓库执行只读查询，返回进程结果；调用方不得直接输出其内容。"""
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, env=git_environment(),
    )


def resolve_roots(root, refs=None):
    """返回待扫描对象 ID；默认包括全部 refs 和可解析的 HEAD，空仓库返回空集合。"""
    if refs is not None:
        if not refs:
            raise HistoryReadError()
        roots = set()
        for reference in refs:
            if reference != "HEAD" and not reference.startswith("refs/"):
                raise HistoryReadError()
            if reference != "HEAD" and git_result(root, "check-ref-format", reference).returncode:
                raise HistoryReadError()
            result = git_result(root, "rev-parse", "--verify", "--end-of-options", reference)
            object_id = result.stdout.strip()
            if result.returncode or result.stderr or not OBJECT_ID.fullmatch(object_id):
                raise HistoryReadError()
            roots.add(object_id)
        return roots
    result = git_result(root, "for-each-ref", "--format=%(objectname)")
    if result.returncode or result.stderr:
        raise HistoryReadError()
    roots = set(result.stdout.splitlines())
    if any(not OBJECT_ID.fullmatch(object_id) for object_id in roots):
        raise HistoryReadError()
    head = git_result(root, "rev-parse", "--verify", "--quiet", "HEAD")
    if head.returncode == 0:
        object_id = head.stdout.strip()
        if not OBJECT_ID.fullmatch(object_id):
            raise HistoryReadError()
        roots.add(object_id)
    elif head.returncode != 1 or head.stderr:
        raise HistoryReadError()
    return roots


class ObjectReader:
    """复用一个 cat-file 子进程，并用有界缓存减少重复对象读取。"""

    def __init__(self, root):
        """保存仓库路径；启动进程前不读取对象。"""
        self.root = root
        self.process = None
        self.cache = OrderedDict()
        self.cache_size = 0

    def __enter__(self):
        """启动只读批量读取进程，返回当前读取器。"""
        self.process = subprocess.Popen(
            ["git", "-C", str(self.root), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=git_environment(),
        )
        return self

    def __exit__(self, *_exception):
        """关闭管道并等待进程退出；异常路径也不会留下子进程。"""
        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self.process.stdout.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def read(self, object_id):
        """按完整对象 ID 返回类型和原始字节；缺失或损坏的响应统一报错。"""
        if object_id in self.cache:
            self.cache.move_to_end(object_id)
            return self.cache[object_id]
        if not OBJECT_ID.fullmatch(object_id):
            raise HistoryReadError()
        self.process.stdin.write(object_id + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline().split()
        if len(header) != 3 or header[0] != object_id or not header[2].isdigit():
            raise HistoryReadError()
        size = int(header[2])
        data = self.process.stdout.read(size)
        if len(data) != size or self.process.stdout.read(1) != b"\n":
            raise HistoryReadError()
        result = (header[1], data)
        if size <= CACHE_BYTES:
            while self.cache and self.cache_size + size > CACHE_BYTES:
                _key, (_kind, removed) = self.cache.popitem(last=False)
                self.cache_size -= len(removed)
            self.cache[object_id] = result
            self.cache_size += size
        return result


def parse_headers(data):
    """分离 commit/tag 的头和正文，返回带行号的头字段及正文。"""
    header, separator, message = data.partition(b"\n\n")
    if not separator:
        raise HistoryReadError()
    fields = []
    for line_number, line in enumerate(header.splitlines(), 1):
        if line.startswith(b" "):
            continue
        key, separator, value = line.partition(b" ")
        if not separator:
            raise HistoryReadError()
        fields.append((key, value, line_number))
    return fields, message


def metadata_issues(object_id, kind, fields):
    """检查提交和标签的身份邮箱，仅返回字段位置及固定问题类别。"""
    names = {b"author": "作者", b"committer": "提交者"} if kind == b"commit" else {b"tagger": "标签作者"}
    issues = []
    for key, label in names.items():
        entries = [(value, line) for name, value, line in fields if name == key]
        if len(entries) != 1:
            issues.append((object_id, "@metadata.txt", 1, label + "身份字段无效"))
            continue
        signature, line = entries[0]
        match = SIGNATURE.fullmatch(signature)
        if not match or not NOREPLY_EMAIL.fullmatch(match.group(1)):
            issues.append((object_id, "@metadata.txt", line, label + "邮箱不是 GitHub noreply"))
    return issues


def attribution_lines(message):
    """返回可识别署名的行号和文本；跳过代码和引用，仅去掉展示用的 Markdown 标记。"""
    fence = None
    for number, original in enumerate(message.decode("utf-8", errors="replace").splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", original)
        if marker:
            current, following = marker.groups()
            if fence is None:
                fence = current
            elif current[0] == fence[0] and len(current) >= len(fence) and not following.strip():
                fence = None
            continue
        if fence is not None or original.startswith(("    ", "\t")) or original.lstrip().startswith(">"):
            continue
        line = re.sub(r"\[([^\]\n]+)\]\([^\n)]*\)", r"\1", original.strip())
        line = re.sub(r"(?<!\w)(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", line).strip()
        line = re.sub(r"^🤖\ufe0f?\s*", "", line)
        yield number, line.encode("utf-8")


def message_issues(path, message):
    """检查说明正文和署名行，返回不带原文和身份信息的问题位置。"""
    issues = RELEASE.check_file(path, message, set())
    for number, line in attribution_lines(message):
        trailer = IDENTITY_TRAILER.fullmatch(line)
        if trailer:
            signature = TRAILER_SIGNATURE.fullmatch(trailer.group(1))
            if signature and AI_ATTRIBUTION_NAME.fullmatch(signature.group("name").strip()):
                issues.append((path, number, "禁止 AI 署名"))
            if not signature or not NOREPLY_EMAIL.fullmatch(signature.group("email")):
                issues.append((path, number, "说明署名邮箱不是 GitHub noreply"))
        generated = GENERATION_ATTRIBUTION.fullmatch(line)
        if generated and AI_ATTRIBUTION_NAME.fullmatch(generated.group("name").strip()):
            issues.append((path, number, "禁止 AI 署名"))
    return issues


def tree_entries(reader, tree_id, parsed_trees):
    """返回 tree 的直接条目；缓存解析结果，正确处理文件名中的换行和空格。"""
    if tree_id in parsed_trees:
        return parsed_trees[tree_id]
    kind, data = reader.read(tree_id)
    if kind != b"tree":
        raise HistoryReadError()
    entries = []
    offset = 0
    hash_size = len(tree_id) // 2
    while offset < len(data):
        end = data.find(b"\0", offset)
        if end < 0 or end + 1 + hash_size > len(data):
            raise HistoryReadError()
        mode, separator, name = data[offset:end].partition(b" ")
        if not separator or not name or name in {b".", b".."} or b"/" in name:
            raise HistoryReadError()
        object_id = data[end + 1:end + 1 + hash_size].hex().encode("ascii")
        entries.append((mode, name.decode("utf-8", errors="surrogateescape"), object_id))
        offset = end + 1 + hash_size
    parsed_trees[tree_id] = entries
    return entries


def flatten_tree(reader, tree_id, parsed_trees):
    """展开一个历史快照，返回仓库相对路径、模式和对象 ID。"""
    files = []
    pending = [(tree_id, "")]
    while pending:
        current, prefix = pending.pop()
        for mode, name, object_id in tree_entries(reader, current, parsed_trees):
            path = prefix + name
            if mode in {b"40000", b"040000"}:
                pending.append((object_id, path + "/"))
            elif mode in {b"100644", b"100755", b"120000", b"160000"}:
                files.append((path, mode, object_id))
            else:
                raise HistoryReadError()
    return files


def inspect_tree(reader, tree_id, parsed_trees, checked_files):
    """复用发布扫描器检查一个快照；链接只能指向该快照中可发布的文件。"""
    files = flatten_tree(reader, tree_id, parsed_trees)
    published = {path for path, _mode, _object_id in files if not RELEASE.forbidden_path(path)}
    issues = []
    for path, mode, object_id in files:
        if RELEASE.forbidden_path(path):
            issues.append((tree_id, path, 1, "禁止发布的本地文件或产物"))
            continue
        if mode == b"160000":
            issues.append((tree_id, path, 1, "无法检查子模块引用的内容"))
            continue
        # Markdown 的同一 blob 在不同快照下可能有不同的链接结果。
        context = tree_id if Path(path).suffix.lower() == ".md" else None
        cache_key = (path, object_id, context)
        if cache_key not in checked_files:
            kind, data = reader.read(object_id)
            if kind != b"blob":
                raise HistoryReadError()
            checked_files[cache_key] = RELEASE.check_file(path, data, published)
        issues.extend((tree_id, *issue) for issue in checked_files[cache_key])
    return issues


def scan_objects(root, roots):
    """扫描引用可达的提交、标签和独立 tree/blob，返回脱敏检查结果。"""
    issues = []
    visited = set()
    checked_trees = set()
    parsed_trees = {}
    checked_files = {}
    pending = list(roots)
    with ObjectReader(root) as reader:
        while pending:
            object_id = pending.pop()
            if object_id in visited:
                continue
            visited.add(object_id)
            kind, data = reader.read(object_id)
            if kind in {b"commit", b"tag"}:
                fields, message = parse_headers(data)
                issues.extend(metadata_issues(object_id, kind, fields))
                header = data.partition(b"\n\n")[0] + b"\n"
                issues.extend((object_id, *issue) for issue in RELEASE.check_file("@metadata.txt", header, set()))
                message_path = "@commit-message.txt" if kind == b"commit" else "@tag-message.txt"
                issues.extend((object_id, *issue) for issue in message_issues(message_path, message))
                if kind == b"tag":
                    targets = [value for key, value, _line in fields if key == b"object"]
                    if len(targets) != 1:
                        raise HistoryReadError()
                    pending.extend(targets)
                    continue
                trees = [value for key, value, _line in fields if key == b"tree"]
                if len(trees) != 1:
                    raise HistoryReadError()
                tree_id = trees[0]
                pending.extend(value for key, value, _line in fields if key == b"parent")
            elif kind == b"tree":
                tree_id = object_id
            elif kind == b"blob":
                issues.append((object_id, "@blob.txt", 1, "独立文件引用缺少发布路径上下文"))
                issues.extend((object_id, *issue) for issue in RELEASE.check_file("@blob.txt", data, set()))
                continue
            else:
                raise HistoryReadError()
            if tree_id not in checked_trees:
                issues.extend(inspect_tree(reader, tree_id, parsed_trees, checked_files))
                checked_trees.add(tree_id)
    return sorted(set(issues))


def check_repository(root, refs=None):
    """检查指定仓库的全部本地历史；refs 非空时只检查明确选中的完整引用。"""
    roots = resolve_roots(root, refs)
    return scan_objects(root, roots) if roots else []


def report_issues(issues):
    """输出对象短 ID、相对路径、行号和类别，不输出身份或命中内容。"""
    for object_id, path, line, category in issues:
        short_id = object_id[:SHORT_ID_LENGTH].decode("ascii")
        print(f"{short_id} {json.dumps(path, ensure_ascii=True)}:{line}: {category}")


def main(argv=None):
    """解析只读检查参数，成功返回 0，历史不合规或无法读取返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT, help="待检查的 Git 仓库")
    parser.add_argument("--refs", nargs="+", help="只检查明确指定的完整引用名或 HEAD；默认检查全部引用")
    args = parser.parse_args(argv)
    try:
        roots = resolve_roots(args.repo, args.refs)
        if not roots:
            print('\".\":0: 没有可检查的本地引用')
            return 0
        issues = scan_objects(args.repo, roots)
    except (OSError, ValueError):
        print('\".\":0: 无法完整读取 Git 历史')
        return 1
    report_issues(issues)
    return int(bool(issues))


if __name__ == "__main__":
    sys.exit(main())

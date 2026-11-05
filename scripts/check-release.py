"""检查暂存区或准备公开的文件，只报告文件、行号和问题类型。"""

import argparse
import ast
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS = {
    "private", ".local", "_deps", "out", ".vs", ".idea", ".vscode", ".cache", "__pycache__", "node_modules", "vcpkg_installed",
    ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".credentials", "backup", "backups", ".backup", ".backups",
    "dumps", "exports", "raw-data", "raw_data", "data-exports", "data_exports",
}
FORBIDDEN_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "cmakeuserpresets.json", ".netrc", "_netrc", ".my.cnf",
    ".mylogin.cnf", ".pgpass", "kubeconfig", "credentials", "credentials.json", "credentials.yml", "credentials.yaml",
    "secrets.json", "secrets.yml", "secrets.yaml", "service-account.json", "service_account.json",
    "application_default_credentials.json", ".git-credentials",
}
LOCAL_CONFIG = re.compile(r"(?:^|[._-])local\.(?:jsonc?|ya?ml|toml|ini|conf|cfg|env)$")
DATA_EXPORT = re.compile(r"(?:^|[._-])(?:backup|dump|mysqldump|export|snapshot)(?:[._-]|$)")
PUBLIC_SAMPLE = re.compile(r"(?:^|[._-])(?:example|sample|fixture)(?:[._-]|$)")
FORBIDDEN_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cert", ".cer", ".der", ".csr", ".p7b", ".p7c", ".ppk", ".jks", ".keystore",
    ".db", ".db3", ".sqlite", ".sqlite3", ".mdb", ".ibd", ".myd", ".myi", ".dump", ".dmp", ".log",
    ".exe", ".dll", ".lib", ".a", ".o", ".obj", ".pdb", ".ilk", ".exp", ".pch", ".gch",
    ".so", ".dylib", ".class", ".pyc", ".pyo", ".wasm",
    ".zip", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".tgz", ".rar", ".nupkg",
    ".bak", ".backup", ".old", ".orig", ".rej", ".swp", ".swo", ".user", ".suo",
}
BINARY_ASSETS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".rtf", ".woff", ".woff2", ".ttf"}
SECRET_PATTERNS = (
    ("疑似私钥", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("疑似 GitHub 令牌", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("疑似 OpenAI 密钥", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("疑似 AWS 访问标识", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("疑似腾讯云访问标识", re.compile(r"\bAKID[A-Za-z0-9]{32}\b")),
    ("疑似 JWT 字面量", re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
)
CREDENTIAL_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:([^\s/@]+)@")
DYNAMIC_PASSWORD = re.compile(
    r"(?:\$\{[A-Za-z_]\w*(?::?\?[^{}]*|:?[+-])?\}|"
    r'''\$\{\{\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[['"][A-Za-z_]\w*['"]\])+\s*\}\}|'''
    r"\$env:[A-Za-z_]\w*|\$[A-Za-z_]\w*|\{[A-Za-z_]\w*\}|%[A-Za-z_]\w*%)"
)
CONFIG_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
JSON_SEPARATOR = re.compile(r"\s*:\s*")
CONFIG_KEY = re.compile(r'''(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[A-Za-z_][\w.-]*)\s*(?P<separator>[:=])''')
CONFIG_QUOTES = re.compile(r'''"(?:[^"\\]|\\.)*"|'(?:[^']|'')*' ''', re.VERBOSE)
REFERENCE_PROPERTY = re.compile(r'''[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[['"][A-Za-z_]\w*['"]\])+''')
MAX_REFERENCE_DEPTH = 16
SECRET_NAME = re.compile(r"(?:password|passwd|pwd|secret|secretkey|secretid|apikey|accesskey|accesskeyid|authtoken|accesstoken|refreshtoken|token|privatekey)$")
PLACEHOLDER = re.compile(
    r"(?:<[^<>\r\n]+>|\*+|\.{3}|(?:change|replace)[_-]?me(?:[_-].*)?|"
    r"(?:your|example|sample|dummy|placeholder)(?:[_-][A-Za-z0-9_-]+)?|"
    r"(?:redacted|not[_-]a[_-]secret|todo|changethis))", re.IGNORECASE,
)
MACHINE_PATH = re.compile(
    r"\b[A-Za-z]:[\\/](?:Users[\\/]|(?:[^\s\"'<>`]*[\\/])?MyCode[\\/])|/(?:Users|home)/[^/\s]+",
    re.IGNORECASE,
)
INLINE_LINK = re.compile(r"\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[\"'][^\n]*?[\"'])?\s*\)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>\n]+)>|(\S+))")


def git_bytes(root, *arguments):
    """在 root 仓库执行 Git 只读查询，返回原始字节；失败时不输出 Git 原文。"""
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True)
    if result.returncode:
        raise ValueError("Git 读取失败")
    return result.stdout


def git_paths(root, *arguments):
    """执行带 -z 的 Git 路径查询，返回去重后的路径，避免解析 Git 展示用的引号。"""
    return {item.decode("utf-8", errors="surrogateescape") for item in git_bytes(root, *arguments).split(b"\0") if item}


def git_modes(root):
    """读取暂存区模式，识别真实 Git 符号链接和子模块；未合并的索引拒绝检查。"""
    modes = {}
    for entry in git_bytes(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != b"0":
            raise ValueError("暂存区文件模式无效或尚未合并")
        modes[path.decode("utf-8", errors="surrogateescape")] = fields[0]
    return modes


def forbidden_path(path):
    """检查仓库相对路径，判断它是否属于本地配置、凭据或生成文件。"""
    parts = path.lower().split("/")
    name = parts[-1]
    if any(part in FORBIDDEN_DIRS or part.startswith("build") for part in parts[:-1]):
        return True
    if name.startswith(".env") and (name == ".env" or name.startswith(".env.")):
        return name not in {".env.example", ".env.sample"}
    if path.lower() == "config/gamehub.json" or name in FORBIDDEN_NAMES or LOCAL_CONFIG.search(name):
        return True
    if name.endswith("~") or any(left == "data" and right == "raw" for left, right in zip(parts[:-2], parts[1:-1])):
        return True
    if (Path(name).suffix in {".sql", ".csv", ".tsv", ".jsonl", ".ndjson"}
            and "migrations" not in parts[:-1] and DATA_EXPORT.search(name)
            and not ("examples" in parts[:-1] and PUBLIC_SAMPLE.search(name))):
        return True
    return (Path(name).suffix in FORBIDDEN_SUFFIXES or bool(re.search(r"\.so\.\d", name))
            or bool(re.search(r"\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$", name)))


def local_link_exists(source, target, published):
    """检查 Markdown 链接；本地目标需要包含在发布文件中，外链直接通过。"""
    if target.startswith(("#", "//")):
        return True
    if re.match(r"^[A-Za-z]:", target) or target.startswith(("file:", "\\\\")):
        return False
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    if parsed.scheme:
        return True
    relative = unquote(parsed.path).replace("\\", "/")
    if not relative:
        return True
    base = "" if relative.startswith("/") else posixpath.dirname(source)
    normalized = posixpath.normpath(posixpath.join(base, relative.lstrip("/")))
    if normalized == ".." or normalized.startswith("../"):
        return False
    if forbidden_path(normalized) or any(part in FORBIDDEN_DIRS for part in normalized.lower().split("/")):
        return False
    return normalized == "." or normalized in published or any(item.startswith(normalized.rstrip("/") + "/") for item in published)


def markdown_issues(path, text, published):
    """检查文本中的本地链接，返回失效链接的行号和问题类型，跳过代码块。"""
    issues = []
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            current = marker.group(1)
            if fence is None:
                fence = current
            elif current[0] == fence[0] and len(current) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        line = re.sub(r"`[^`\n]*`", "", line)
        links = list(INLINE_LINK.finditer(line))
        reference = REFERENCE_LINK.match(line)
        if reference:
            links.append(reference)
        for match in links:
            if not local_link_exists(path, match.group(1) or match.group(2), published):
                issues.append((path, number, "本地链接目标未纳入发布文件"))
    return issues


def reject_json_constant(_value):
    """JSON 不接受 NaN/Infinity，报错时不带输入内容。"""
    raise ValueError("JSON 常量无效")


def credential_key(key):
    """将配置键归一化，返回它是否指向密码、令牌或云凭据本身。"""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return bool(SECRET_NAME.search(normalized))


def static_credential(key, value):
    """根据配置键和值判断是否写死凭据；环境引用、空值和完整占位符返回 False。"""
    if not credential_key(key) or value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (str, int, float)):
        return False
    value = str(value).strip()
    return bool(value) and not dynamic_credential(value) and not PLACEHOLDER.fullmatch(value)


def dynamic_credential(value, depth=0):
    """判断值是否为动态引用；默认分支中的静态密码仍需拦截，错误说明可以保留。"""
    if depth >= MAX_REFERENCE_DEPTH:
        return False
    if DYNAMIC_PASSWORD.fullmatch(value):
        return True
    if value.startswith("${{") and value.endswith("}}"):
        terms = re.split(r"\s*(?:\|\||&&)\s*", value[3:-2].strip())
        return bool(terms) and all(REFERENCE_PROPERTY.fullmatch(term) for term in terms)
    default = re.fullmatch(r"\$\{[A-Za-z_]\w*:?[+-](.*)\}", value, re.DOTALL)
    return bool(default and (PLACEHOLDER.fullmatch(default.group(1)) or dynamic_credential(default.group(1), depth + 1)))


def reference_spans(text):
    """返回平衡的 ${...} 引用范围，嵌套默认引用和引号内花括号不会被误拆。"""
    cursor = 0
    while cursor < len(text):
        start = text.find("${", cursor)
        if start < 0:
            return
        depth = 1
        quote = None
        cursor = start + 2
        while cursor < len(text) and depth:
            character = text[cursor]
            if quote:
                if character == "\\":
                    cursor += 1
                elif character == quote:
                    quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            cursor += 1
        if not depth:
            yield start, cursor


def json_credentials(text):
    """读取 JSON 字符串键后的标量值，返回静态凭据的行号；不执行配置内容。"""
    decoder = json.JSONDecoder(parse_constant=reject_json_constant)
    for match in CONFIG_STRING.finditer(text):
        end = match.end()
        separator = JSON_SEPARATOR.match(text, end)
        if not separator:
            continue
        try:
            key = json.loads(match.group())
            if not credential_key(key):
                continue
            value, _end = decoder.raw_decode(text, separator.end())
        except (ValueError, RecursionError):
            continue
        if static_credential(key, value):
            yield text.count("\n", 0, match.start()) + 1


def config_scalar(raw, flow=False):
    """去掉 YAML/dotenv 标量的引号和注释，返回其文本值，保留引用内部字符。"""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    quoted = CONFIG_QUOTES.match(raw)
    if quoted:
        token = quoted.group()
        remaining = raw[quoted.end():]
        if remaining and not re.fullmatch(r"\s*(?:#.*)?", remaining) and not (flow and remaining.lstrip().startswith((",", "}"))):
            return raw
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        try:
            return json.loads(token)
        except ValueError:
            return token[1:-1]
    # 变量引用中的逗号、井号和花括号属于引用本身，不能截成一段静态密码。
    reference = next(reference_spans(raw), None)
    if reference and reference[0] == 0:
        remaining = raw[reference[1]:]
        if not remaining or re.fullmatch(r"\s+#.*", remaining) or (flow and remaining.lstrip().startswith((",", "}"))):
            return raw[:reference[1]]
    raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    return re.split(r"[,}]", raw, maxsplit=1)[0].strip() if flow else raw


def following_yaml_scalar(lines, start, indent):
    """读取空配置键后下一行开始的缩进标量；子映射和序列返回空值。"""
    parts = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if not parts and (stripped.startswith("- ") or re.match(r'''(?:[\w.-]+|"[^"\n]*"|'[^'\n]*')\s*:(?:\s|$)''', stripped)):
            return ""
        parts.append(stripped)
    return config_scalar(" ".join(parts))


def text_config_credentials(text, dotenv=False):
    """按 YAML/dotenv 赋值上下文返回静态凭据行号，支持流式映射和缩进块标量。"""
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        value_spans = [(match.start(), match.end()) for match in CONFIG_QUOTES.finditer(line)] + list(reference_spans(line))
        for match in CONFIG_KEY.finditer(line):
            if any(start < match.start() < end for start, end in value_spans):
                continue
            prefix = line[:match.start()].strip()
            if prefix and prefix not in {"-", "export"} and not prefix.endswith(("{", ",")):
                continue
            if dotenv and match.group("separator") != "=":
                continue
            key = config_scalar(match.group("key"))
            if not credential_key(key):
                continue
            raw = line[match.end():].strip()
            if not dotenv and (not raw or raw.startswith("#")):
                value = following_yaml_scalar(lines, number, len(line) - len(line.lstrip()))
            elif not dotenv and re.fullmatch(r"[|>][+-]?(?:\s+#.*)?", raw):
                indent = len(line) - len(line.lstrip())
                block = []
                for following in lines[number:]:
                    if following.strip() and len(following) - len(following.lstrip()) <= indent:
                        break
                    block.append(following.strip())
                value = "\n".join(block).strip()
            else:
                value = config_scalar(raw, flow=prefix.endswith(("{", ",")))
                if not dotenv and raw in {"null", "Null", "NULL", "~", "true", "false", "True", "False", "TRUE", "FALSE"}:
                    value = None
            if static_credential(key, value):
                yield number


def configuration_issues(path, text):
    """仅检查 JSON、YAML 和 dotenv 配置，返回行号和类别，避免把程序变量当配置。"""
    name = Path(path).name.lower()
    base_name = re.sub(r"\.(?:example|sample|template)$", "", name)
    if base_name.endswith((".json", ".jsonc")):
        lines = json_credentials(text)
    elif base_name.endswith((".yaml", ".yml")):
        lines = text_config_credentials(text)
    elif base_name == ".env" or base_name.endswith(".env") or base_name.startswith(".env."):
        lines = text_config_credentials(text, dotenv=True)
    else:
        return []
    return [(path, number, "配置中含静态凭据") for number in lines]


def check_file(path, data, published):
    """检查文件内容，返回文件、行号和问题类型，不返回命中的原文。"""
    issues = []
    if forbidden_path(path):
        issues.append((path, 1, "禁止发布的本地文件或产物"))
    # 先容错解码并扫描 ASCII 凭据，不能因为编码错误或二进制后缀就漏查。
    scan_text = data.decode("utf-8", errors="replace")
    for category, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(scan_text):
            issues.append((path, scan_text.count("\n", 0, match.start()) + 1, category))
    for match in CREDENTIAL_URL.finditer(scan_text):
        if not dynamic_credential(match.group(1)):
            issues.append((path, scan_text.count("\n", 0, match.start()) + 1, "连接地址含嵌入式账号密码"))
    for match in MACHINE_PATH.finditer(scan_text):
        issues.append((path, scan_text.count("\n", 0, match.start()) + 1, "机器专有绝对路径"))
    issues.extend(configuration_issues(path, scan_text))
    if Path(path).suffix.lower() in BINARY_ASSETS:
        return issues
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        issues.append((path, data.count(b"\n", 0, error.start) + 1, "不是有效 UTF-8"))
        return issues
    if text.startswith("\ufeff"):
        issues.append((path, 1, "UTF-8 含 BOM"))
    if "\r" in text:
        issues.append((path, text.count("\n", 0, text.index("\r")) + 1, "换行符不是 LF"))
    if text and not text.endswith("\n"):
        issues.append((path, text.count("\n") + 1, "缺少末尾换行"))
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        try:
            json.loads(text, parse_constant=reject_json_constant)
        except json.JSONDecodeError as error:
            issues.append((path, error.lineno, "JSON 语法错误"))
        except (ValueError, RecursionError):
            issues.append((path, 1, "JSON 语法错误"))
    elif suffix == ".py":
        try:
            ast.parse(text, filename=path)
        except (SyntaxError, ValueError, RecursionError) as error:
            issues.append((path, getattr(error, "lineno", None) or 1, "Python 语法错误"))
    elif suffix == ".md":
        issues.extend(markdown_issues(path, text, published))
    return issues


def check_repository(root, all_files=False):
    """返回仓库的检查结果；默认读取暂存内容，全量模式读取工作区中的候选文件。"""
    tracked = git_paths(root, "ls-files", "--cached", "-z")
    modes = git_modes(root)
    if all_files:
        paths = git_paths(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    else:
        paths = git_paths(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    published = {path for path in tracked | paths if not forbidden_path(path)
                 and modes.get(path) not in (b"120000", b"160000")}
    issues = []
    for path in sorted(paths):
        if forbidden_path(path):
            issues.append((path, 1, "禁止发布的本地文件或产物"))
            continue
        if modes.get(path) == b"120000":
            issues.append((path, 1, "发布文件不能是本地符号链接"))
            continue
        if modes.get(path) == b"160000":
            issues.append((path, 1, "无法检查子模块引用的内容"))
            continue
        try:
            if all_files:
                local = root / path
                if local.is_symlink():
                    issues.append((path, 1, "发布文件不能是本地符号链接"))
                    continue
                data = local.read_bytes()
            else:
                data = git_bytes(root, "show", ":" + path)
        except (OSError, ValueError):
            issues.append((path, 1, "无法读取待发布内容"))
            continue
        issues.extend(check_file(path, data, published))
    return sorted(set(issues))


def report_issues(issues):
    """输出文件、行号和问题类型，不输出命中的内容或异常原文。"""
    for path, line, category in issues:
        safe_path = json.dumps(path, ensure_ascii=False)
        print(f"{safe_path}:{line}: {category}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-files", action="store_true", help="检查已跟踪文件和未被忽略的新文件")
    args = parser.parse_args()
    try:
        issues = check_repository(ROOT, args.all_files)
    except (OSError, ValueError):
        issues = [(".", 0, "无法读取 Git 文件集合")]
    report_issues(issues)
    return int(bool(issues))


if __name__ == "__main__":
    sys.exit(main())

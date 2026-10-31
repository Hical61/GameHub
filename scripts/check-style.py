"""检查或整理项目里的 C++ 文件，也可以按编译参数运行 clang-tidy。"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CPP_SUFFIXES = {".c", ".cpp", ".h", ".hpp"}


def resolve_tool(value):
    """查找 LLVM 22，旧版本可能读不了项目配置。"""
    tool = shutil.which(value)
    if tool is None:
        raise ValueError(f"找不到工具：{value}，请指定 LLVM 22 的完整路径")
    result = subprocess.run(
        [tool, "--version"], capture_output=True, text=True, check=True
    )
    if not re.search(r"version 22\.", result.stdout):
        raise ValueError(f"需要 LLVM 22：{value}\n{result.stdout.strip()}")
    return tool


def cpp_files():
    """只检查 src 和 tests 里的 C++ 文件。"""
    return sorted(
        path.resolve()
        for folder in (ROOT / "src", ROOT / "tests")
        for path in folder.rglob("*")
        if path.is_file() and path.suffix in CPP_SUFFIXES
    )


def compilation_units(build_dir, sources):
    """从编译数据库里挑出 GameHub 文件，不分析 Hical 和其他依赖。"""
    database = build_dir / "compile_commands.json"
    if not database.is_file():
        raise ValueError("缺少 compile_commands.json，请先使用 Ninja 或 Makefiles 配置项目")
    units = set()
    for entry in json.loads(database.read_text(encoding="utf-8")):
        path = Path(entry["file"])
        if not path.is_absolute():
            path = Path(entry["directory"]) / path
        path = path.resolve()
        if path in sources:
            units.add(path)
    if not units:
        raise ValueError("编译数据库中没有 GameHub 编译单元，请重新运行 CMake 配置")
    return sorted(units)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="按 .clang-format 整理项目里的 C++ 文件")
    parser.add_argument("--clang-format", default="clang-format", help="LLVM 22 clang-format 路径")
    parser.add_argument("--tidy", action="store_true", help="同时运行 clang-tidy，不自动修复")
    parser.add_argument("--clang-tidy", default="clang-tidy", help="LLVM 22 clang-tidy 路径")
    parser.add_argument("--build-dir", type=Path, help="包含 compile_commands.json 的构建目录")
    args = parser.parse_args()
    try:
        formatter = resolve_tool(args.clang_format)
        sources = cpp_files()
        tidy = resolve_tool(args.clang_tidy) if args.tidy else None
        if args.tidy and args.build_dir is None:
            raise ValueError("--tidy 需要 --build-dir")
        build_dir = args.build_dir.resolve() if args.build_dir is not None else None
        units = compilation_units(build_dir, set(sources)) if args.tidy else []
        command = [formatter, "-i"] if args.fix else [formatter, "--dry-run", "--Werror"]
        # 直接指定根目录配置，防止子目录的规则覆盖它。
        command += [f"--style=file:{ROOT / '.clang-format'}"]
        result = subprocess.run(command + [str(path) for path in sources], cwd=ROOT)
        if result.returncode:
            return result.returncode
        print(f"C++ 格式{'整理' if args.fix else '检查'}通过：{len(sources)} 个文件", flush=True)
        for index, path in enumerate(units, 1):
            print(f"clang-tidy [{index}/{len(units)}] {path.relative_to(ROOT)}", flush=True)
            result = subprocess.run(
                [tidy, str(path), "-p", str(build_dir), "--quiet"], cwd=ROOT
            )
            if result.returncode:
                return result.returncode
        if args.tidy:
            print("clang-tidy 已跑完，告警需要人工检查；有告警时此命令仍会返回成功", flush=True)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"检查失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

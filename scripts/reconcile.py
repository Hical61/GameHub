"""比较渠道、Hub 和游戏服导出的合成流水，报告差异，不执行补发或数据库写入。"""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sys


IDENTITY = ("gameId", "channel", "channelOrderId")
VALUE_FIELDS = ("serverId", "amountMinor", "currency")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 100_000


def load_records(path, require_status=False):
    """path 为 UTF-8 JSON 数组或 CSV；返回字段已检查的记录，错误不回显原流水。"""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("对账输入超过 32 MiB")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream)) if path.suffix.lower() == ".csv" else json.load(stream)
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("对账输入必须是最多十万条记录的数组或 CSV")
    normalized = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("流水记录必须是对象")
        value = {}
        for key in (*IDENTITY, "currency"):
            field = row.get(key)
            if not isinstance(field, str) or not field or len(field) > 96 or "\0" in field:
                raise ValueError("流水标识或币种格式错误")
            value[key] = field
        for key in ("serverId", "amountMinor"):
            field = row.get(key)
            if isinstance(field, bool) or not isinstance(field, (str, int)) or not str(field).isascii() or not str(field).isdigit():
                raise ValueError("流水金额和区服必须是正整数")
            value[key] = int(field)
            if not 1 <= value[key] <= (1_000_000_000_000 if key == "amountMinor" else 0xffffffff):
                raise ValueError("流水金额或区服超出范围")
        if len(value["currency"]) != 3 or not all("A" <= char <= "Z" for char in value["currency"]):
            raise ValueError("币种必须是三个大写 ASCII 字母")
        value["status"] = str(row.get("status", ""))
        if value["status"] not in ("", "pending", "delivering", "done", "failed"):
            raise ValueError("Hub 状态必须是 pending、delivering、done 或 failed")
        if require_status and not value["status"]:
            raise ValueError("Hub 导出必须包含投递状态，不能用缺失状态判断确认丢失")
        normalized.append(value)
    return normalized


def identity(row):
    """row 为已检查的流水；返回结构化订单身份，不拼接字符串。"""
    return tuple(row[key] for key in IDENTITY)


def reference(key):
    """返回稳定摘要，公开诊断不输出原始订单号或玩家字段。"""
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def reconcile(channel, hub, ledger):
    """比较三份已检查的记录；返回差异，confirmed 字段不能替代游戏服权威账本。"""
    sources = {}
    for name, rows in (("channel", channel), ("hub", hub), ("ledger", ledger)):
        grouped = defaultdict(list)
        for row in rows:
            grouped[identity(row)].append(row)
        sources[name] = grouped
    issues = []
    for key in sorted(set().union(*(set(rows) for rows in sources.values()))):
        records = {name: rows.get(key, []) for name, rows in sources.items()}
        def add(category, **details):
            issues.append({"reference": reference(key), "category": category, **details})
        for name, rows in records.items():
            if len(rows) > 1:
                add("duplicate_record", source=name, count=len(rows))
        channel_rows, hub_rows, ledger_rows = (records[name] for name in ("channel", "hub", "ledger"))
        if channel_rows and not hub_rows:
            add("channel_missing_in_hub")
        if hub_rows and not channel_rows:
            add("hub_missing_in_channel")
        if ledger_rows and not hub_rows:
            add("ledger_missing_in_hub")
        all_values = {tuple(row[field] for field in VALUE_FIELDS) for rows in records.values() for row in rows}
        if len(all_values) > 1:
            add("amount_currency_or_target_conflict")
        if hub_rows and not ledger_rows:
            if any(row["status"] == "done" for row in hub_rows):
                add("hub_done_without_ledger")
            else:
                add("hub_without_ledger")
        if ledger_rows and hub_rows and any(row["status"] != "done" for row in hub_rows):
            add("ledger_committed_confirmation_unresolved")
    return {"formatVersion": 1, "counts": {name: sum(len(rows) for rows in grouped.values())
            for name, grouped in sources.items()}, "issueCount": len(issues),
            "categoryCounts": dict(sorted(Counter(item["category"] for item in issues).items())), "issues": issues}


def write_demo(directory):
    """directory 接收三份只含合成数据的 JSON；已有文件不覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    base = {"gameId": "demo", "channel": "synthetic", "serverId": 1, "amountMinor": 100, "currency": "CNY"}
    good = dict(base, channelOrderId="synthetic-good")
    missing = dict(base, channelOrderId="synthetic-missing-in-hub")
    ack_lost = dict(base, channelOrderId="synthetic-confirmation-lost")
    mismatch = dict(base, channelOrderId="synthetic-amount-conflict")
    content = {"channel.json": [good, missing, ack_lost, mismatch],
               "hub.json": [dict(good, status="done"), dict(ack_lost, status="failed"), dict(mismatch, status="done")],
               "ledger.json": [good, ack_lost, dict(mismatch, amountMinor=200)]}
    if any((directory / name).exists() for name in content):
        raise ValueError("示例目录已有同名文件，请换一个空目录")
    for name, rows in content.items():
        (directory / name).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def validate_output(output, inputs):
    """output 不得指向任何输入，包括符号链接和硬链接；冲突时在写文件前拒绝。"""
    resolved = output.resolve()
    for source in inputs:
        if resolved == source.resolve() or (output.exists() and source.exists() and output.samefile(source)):
            raise ValueError("差异报告不能覆盖输入流水，请指定另一个输出路径")


def main():
    """执行只读对账；返回 0 无差异、2 有差异、1 输入或写报告失败。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=Path, help="渠道导出，JSON 或 CSV")
    parser.add_argument("--hub", type=Path, help="Hub 导出，包含投递 status")
    parser.add_argument("--ledger", type=Path, help="游戏服权威到账流水")
    parser.add_argument("--output", type=Path, help="差异报告，仅包含订单身份摘要")
    parser.add_argument("--demo-dir", type=Path, help="创建合成示例，不读取业务数据")
    args = parser.parse_args()
    try:
        if args.demo_dir:
            write_demo(args.demo_dir)
            print("合成对账示例已生成")
            return 0
        if not all((args.channel, args.hub, args.ledger, args.output)):
            parser.error("请提供 --channel、--hub、--ledger 和 --output")
        validate_output(args.output, (args.channel, args.hub, args.ledger))
        report = reconcile(load_records(args.channel), load_records(args.hub, require_status=True),
                           load_records(args.ledger))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"对账完成：发现 {report['issueCount']} 项差异，报告只含身份摘要，不会自动补发")
        return 2 if report["issueCount"] else 0
    except (OSError, ValueError, csv.Error):
        print("对账失败，请检查文件格式、字段和输入范围", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

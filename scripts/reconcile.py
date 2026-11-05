"""比较渠道、Hub 和游戏服导出的合成流水，报告差异，不执行补发或数据库写入。"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone


IDENTITY = ("gameId", "channel", "channelOrderId")
HUB_IDENTITY = ("gameId", "taskType", "namespace", "bizKey", "serverId")
VALUE_FIELDS = ("serverId", "amountMinor", "currency")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 100_000


def load_export(path, require_status=False):
    """path 为 UTF-8 JSON 数组或 CSV；返回字段已检查的记录，错误不回显原流水。"""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("对账输入超过 32 MiB")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream)) if path.suffix.lower() == ".csv" else json.load(stream)
    metadata = {}
    if isinstance(records, dict):
        if records.get("formatVersion") != 2:
            raise ValueError("导出封装格式版本必须为 2")
        metadata = {key: records.get(key) for key in ("windowStart", "windowEnd", "watermark")}
        validate_window(metadata)
        records = records.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("对账输入必须是最多十万条记录的数组或 CSV")
    normalized = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("流水记录必须是对象")
        value = {}
        new_identity = any(key in row for key in HUB_IDENTITY[1:4])
        identity_fields = HUB_IDENTITY[:-1] if new_identity else IDENTITY
        if new_identity and any(key in row for key in IDENTITY[1:]):
            raise ValueError("同一记录不能混合 Hub 业务身份与渠道订单身份")
        for key in (*identity_fields, "currency"):
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
        value["identityMode"] = "hub-v2" if new_identity else "legacy-channel-v1"
        normalized.append(value)
    return normalized, metadata


def load_records(path, require_status=False):
    """读取标准化文件记录；需要对账时间窗时使用 load_export。"""
    return load_export(path, require_status)[0]


def validate_window(metadata):
    """检查 UTC 时间窗和已完成水位；不完整账期拒绝比较，避免把延迟记录当缺单。"""
    try:
        values = [datetime.fromisoformat(metadata[key].replace("Z", "+00:00"))
                  for key in ("windowStart", "windowEnd", "watermark")]
        if any(value.tzinfo is None or value.utcoffset().total_seconds() != 0 for value in values):
            raise ValueError
        if not values[0] < values[1] <= values[2]:
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("时间窗必须为 UTC，开始早于结束，完整水位不得早于结束") from None


def identity(row):
    """row 为已检查的流水；返回结构化订单身份，不拼接字符串。"""
    mode = row.get("identityMode", "hub-v2" if "bizKey" in row else "legacy-channel-v1")
    return (mode, *(row[key] for key in (HUB_IDENTITY if mode == "hub-v2" else IDENTITY)))


def reference(key):
    """返回稳定摘要，公开诊断不输出原始订单号或玩家字段。"""
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def reconcile(channel, hub, ledger, metadata=None):
    """比较三份已检查的记录；返回差异，confirmed 字段不能替代游戏服权威账本。"""
    modes = {identity(row)[0] for rows in (channel, hub, ledger) for row in rows}
    if len(modes) > 1:
        raise ValueError("Hub 业务身份与渠道订单身份必须分开对账，不允许混合或猜测映射")
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
    return {"formatVersion": 2, "identityMode": next(iter(modes), "hub-v2"),
            "window": metadata or None, "windowValidated": bool(metadata),
            "counts": {name: sum(len(rows) for rows in grouped.values())
            for name, grouped in sources.items()}, "issueCount": len(issues),
            "categoryCounts": dict(sorted(Counter(item["category"] for item in issues).items())), "issues": issues}


def record_cases(path, report, disposition=None):
    """将摘要差异登记到本地 SQLite；同份报告重复执行不重复计数，不保存业务载荷。"""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("CREATE TABLE IF NOT EXISTS runs (digest TEXT PRIMARY KEY);"
            "CREATE TABLE IF NOT EXISTS cases (reference TEXT NOT NULL, category TEXT NOT NULL,"
            "source TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open', first_seen TEXT NOT NULL,"
            "last_seen TEXT NOT NULL, observations INTEGER NOT NULL DEFAULT 1,"
            "PRIMARY KEY(reference,category,source));")
        if disposition:
            reference_value, category, state = disposition
            if len(reference_value) != 64 or any(char not in "0123456789abcdef" for char in reference_value):
                raise ValueError("处置引用必须是报告中的 SHA-256 摘要")
            changed = connection.execute("UPDATE cases SET state=? WHERE reference=? AND category=?",
                                         (state, reference_value, category)).rowcount
            if not changed:
                raise ValueError("差异处置目标不存在")
            return
        digest = reference(report)
        if not connection.execute("INSERT OR IGNORE INTO runs VALUES(?)", (digest,)).rowcount:
            return
        now = datetime.now(timezone.utc).isoformat()
        for issue in report["issues"]:
            connection.execute("INSERT INTO cases(reference,category,source,first_seen,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(reference,category,source) DO UPDATE SET last_seen=excluded.last_seen,"
                "observations=cases.observations+1", (issue["reference"], issue["category"],
                issue.get("source", ""), now, now))


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
    parser.add_argument("--case-ledger", type=Path, help="本地差异处置 SQLite，仅存身份摘要")
    parser.add_argument("--case-reference", help="登记处置状态的身份摘要")
    parser.add_argument("--case-category", help="登记处置状态的差异类别")
    parser.add_argument("--case-state", choices=("open", "investigating", "resolved"))
    args = parser.parse_args()
    try:
        if args.demo_dir:
            write_demo(args.demo_dir)
            print("合成对账示例已生成")
            return 0
        if any((args.case_reference, args.case_category, args.case_state)):
            if not all((args.case_ledger, args.case_reference, args.case_category, args.case_state)):
                parser.error("处置操作需要 --case-ledger、--case-reference、--case-category 和 --case-state")
            record_cases(args.case_ledger, None, (args.case_reference, args.case_category, args.case_state))
            print("差异处置状态已更新，不执行补发")
            return 0
        if not all((args.channel, args.hub, args.ledger, args.output)):
            parser.error("请提供 --channel、--hub、--ledger 和 --output")
        validate_output(args.output, (args.channel, args.hub, args.ledger))
        if args.case_ledger:
            validate_output(args.case_ledger, (args.channel, args.hub, args.ledger, args.output))
        exports = [load_export(args.channel), load_export(args.hub, require_status=True), load_export(args.ledger)]
        windows = [item[1] for item in exports]
        if any(windows) and (not all(windows) or any(
                (item["windowStart"], item["windowEnd"]) != (windows[0]["windowStart"], windows[0]["windowEnd"])
                for item in windows)):
            raise ValueError("三方导出必须提供相同且已完成的时间窗")
        report = reconcile(*(item[0] for item in exports), metadata={
            "windowStart": windows[0]["windowStart"], "windowEnd": windows[0]["windowEnd"],
            "watermarks": dict(zip(("channel", "hub", "ledger"), (item["watermark"] for item in windows)))}
            if all(windows) else None)
        if args.case_ledger:
            record_cases(args.case_ledger, report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"对账完成：发现 {report['issueCount']} 项差异，报告只含身份摘要，不会自动补发")
        return 2 if report["issueCount"] else 0
    except (OSError, ValueError, csv.Error, sqlite3.Error):
        print("对账失败，请检查文件格式、字段和输入范围", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

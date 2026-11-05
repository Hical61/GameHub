"""在两套新建临时 MySQL 中演练逻辑备份和恢复，不接受已有数据库地址。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import integration


def executable(directory, name):
    """directory 是工具目录，name 不含后缀；返回当前系统的可执行文件路径。"""
    return directory / (name + ".exe" if os.name == "nt" else name)


def identity_command(expected_datadir):
    """expected_datadir 是本次目录；错误实例在 mysqldump 的同一会话初始化时直接报错。"""
    expected = expected_datadir.as_posix().rstrip("/").replace("'", "''")
    actual = "TRIM(TRAILING '/' FROM REPLACE(@@datadir,CHAR(92),'/'))"
    compare = f"LOWER({actual})=LOWER('{expected}')" if os.name == "nt" else f"{actual}='{expected}'"
    return "SET @gamehub_backup_identity=IF(" + compare + ",1,JSON_EXTRACT('INVALID GAMEHUB DATABASE IDENTITY','$'))"


def dump_database(fixture, expected_datadir=None, tools_directory=None):
    """只导出当前 Fixture 的 gamehub；返回 SQL 字节，失败时不回显 SQL 或客户端输出。"""
    command = executable(tools_directory or fixture.mysql_bin, "mysqldump")
    help_result = subprocess.run([str(command), "--no-defaults", "--help"], capture_output=True, timeout=15)
    if help_result.returncode or b"--init-command" not in help_result.stdout:
        raise RuntimeError("mysqldump 不支持同会话身份检查，请通过 --dump-tools 指定支持 --init-command 的工具目录")
    result = subprocess.run([str(command), "--no-defaults", "--default-character-set=utf8mb4", "--protocol=TCP", "-h127.0.0.1",
        f"-P{fixture.db_port}", "-uroot", "--single-transaction", "--skip-lock-tables",
        "--set-gtid-purged=OFF", "--skip-column-statistics", "--no-tablespaces",
        "--init-command=" + identity_command(expected_datadir or fixture.expected_datadir), "gamehub"],
        capture_output=True, timeout=30, env=fixture.env)
    if result.returncode:
        raise RuntimeError("临时数据库备份失败，实例身份检查或导出未通过")
    return result.stdout


def fingerprint(fixture):
    """读取所有 Hub 表的行数和内容摘要，报告中不保存原始行。"""
    names = fixture.sql("SHOW FULL TABLES FROM gamehub WHERE Table_type='BASE TABLE'")
    tables = {}
    for entry in names.splitlines():
        name = entry.split("\t")[0]
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise RuntimeError("演练数据库出现非预期表名")
        columns = [line.split("\t")[0] for line in fixture.sql(f"SHOW COLUMNS FROM gamehub.`{name}`").splitlines()]
        if not all(re.fullmatch(r"[A-Za-z0-9_]+", field) for field in columns):
            raise RuntimeError("演练数据库出现非预期列名")
        expression = ",".join("`" + field + "`" for field in columns)
        rows = sorted(fixture.sql(f"SELECT JSON_ARRAY({expression}) FROM gamehub.`{name}`").splitlines())
        tables[name] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}
    return tables


def seed(fixture):
    """写入合成订单、公告和配置，等待确认后停止 Hub，备份期间不再有业务写入。"""
    events = []
    for index in range(2):
        status, body = fixture.request("POST", "/api/callback/demo/demo",
                                       integration.order("backup-synthetic-" + str(index)), signed=True)
        if status != 200:
            raise AssertionError("备份演练订单受理失败")
        events.append(body["eventId"])
    for path, body in (("/admin/announce/demo", {"bizKey": "backup-synthetic-announce", "serverId": 0,
                                               "content": "合成备份演练"}),
                       ("/admin/config/demo/1", {"bizKey": "config:1", "serverId": 0,
                                                "content": {"syntheticSetting": True}})):
        status, response = fixture.request("POST", path, body, admin=fixture.admin())
        if status != 200:
            raise AssertionError("备份演练公告或配置受理失败")
        events.append(response["eventId"])
    integration.wait_for(lambda: all(fixture.tasks(event_id) and all(value == 2 for value in fixture.tasks(event_id))
                                     for event_id in events), "备份演练投递完成", 30)
    if int(fixture.sql("SELECT MAX(version) FROM gamehub.schema_version")) >= 4:
        status, control = fixture.request("POST", "/admin/control/demo", {"expectedRevision": 0,
            "deliveryEnabled": True, "maxConcurrentDeliveries": 2, "maxConcurrentPerServer": 1}, admin=fixture.admin())
        if status != 200 or control.get("revision") != "1":
            raise AssertionError("备份演练未生成共享控制数据")
        content_hash = hashlib.sha256(json.dumps({"syntheticSetting": True}, sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
        status, _ = fixture.request("POST", "/internal/config-applied/demo/1",
            {"version": 1, "contentHash": content_hash}, signed=True)
        if status != 200:
            raise AssertionError("备份演练未生成区服配置应用状态")
        for table in ("delivery_attempt", "game_delivery_control", "config_server_state"):
            if int(fixture.sql("SELECT COUNT(*) FROM gamehub." + table)) < 1:
                raise AssertionError("备份演练缺少投递尝试、共享控制或配置应用数据")
    if int(fixture.sql("SELECT MAX(version) FROM gamehub.schema_version")) >= 2:
        archived = int(events[0])
        fixture.sql(f"UPDATE gamehub.hub_event SET created_at=TIMESTAMPADD(DAY,-3,UTC_TIMESTAMP(6)) WHERE id={archived}; "
                    f"UPDATE gamehub.delivery_task SET updated_at=TIMESTAMPADD(DAY,-3,UTC_TIMESTAMP(6)) WHERE event_id={archived}")
        status, result = fixture.request("POST", "/admin/archive/demo",
            {"keepDays": 1, "batchSize": 1, "redactPayload": True}, admin=fixture.admin())
        if status != 200 or result.get("archivedTasks") != 1 or result.get("redactedEvents") != 1:
            raise AssertionError("备份演练未生成已归档和脱敏的数据")
    fixture._stop_child(fixture.hub)


def verify_restored_writes(fixture):
    """恢复后写入新订单并比较计数，确认触发器仍然工作，也没有恢复时重复累加。"""
    status, value = fixture.request("POST", "/api/callback/demo/demo",
                                    integration.order("backup-after-restore"), signed=True)
    if status != 200:
        raise AssertionError("恢复后的新订单无法受理")
    integration.wait_for(lambda: fixture.tasks(value["eventId"]) == [2], "恢复后的新订单完成", 30)
    if int(fixture.sql("SELECT MAX(version) FROM gamehub.schema_version")) >= 2:
        rows = fixture.sql("SELECT status,COUNT(*) FROM gamehub.delivery_task GROUP BY status")
        counts = [0, 0, 0, 0]
        for line in rows.splitlines():
            state, count = map(int, line.split("\t"))
            counts[state] = count
        archived = int(fixture.sql("SELECT COUNT(*) FROM gamehub.delivery_task_archive"))
        actual = fixture.sql("SELECT SUM(pending_count),SUM(delivering_count),SUM(done_count),"
                             "SUM(failed_count),SUM(archived_count) FROM gamehub.delivery_task_counts")
        if list(map(int, actual.split("\t"))) != counts + [archived]:
            raise AssertionError("恢复后的统计触发器或计数不一致")
    if int(fixture.sql("SELECT MAX(version) FROM gamehub.schema_version")) >= 4:
        status, control = fixture.request("GET", "/admin/control/demo", admin=fixture.admin())
        if status != 200 or control.get("revision") != "1" or control.get("maxConcurrentPerServer") != 1:
            raise AssertionError("恢复后的共享控制与版本不一致")
        status, state = fixture.request("GET", "/admin/config-status/demo/0", admin=fixture.admin())
        if status != 200 or not state.get("items"):
            raise AssertionError("恢复后的区服配置状态缺失")
        if int(fixture.sql("SELECT COUNT(*) FROM gamehub.delivery_attempt WHERE outcome='success'")) < 1:
            raise AssertionError("恢复后的投递尝试记录缺失")


def execute(source, target, dump_tools=None):
    """source/target 都是本次新建实例；返回备份恢复耗时及各表摘要。"""
    source.start()
    seed(source)
    before = fingerprint(source)
    began = time.monotonic()
    dump = dump_database(source, tools_directory=dump_tools)
    backup_seconds = time.monotonic() - began
    # 错误 datadir 必须在同一导出会话被拒绝，不允许获得任何可用 SQL。
    try:
        dump_database(source, source.path / "wrong-instance", tools_directory=dump_tools)
    except RuntimeError:
        pass
    else:
        raise AssertionError("备份未拒绝错误数据库身份")
    target.start()
    target._stop_child(target.hub)
    target.sql("DROP DATABASE gamehub; CREATE DATABASE gamehub CHARACTER SET utf8mb4")
    began = time.monotonic()
    # Fixture.sql 先在同一 mysql 会话检查 datadir，再执行恢复内容。
    target.sql("USE gamehub;\n" + dump.decode("utf-8"))
    restore_seconds = time.monotonic() - began
    restored = fingerprint(target)
    if before != restored:
        raise AssertionError("备份恢复后的表内容不一致")
    target.start_hub()
    # 恢复后的相同订单必须返回原事件，不创建新投递任务。
    original = source.sql("SELECT id FROM gamehub.hub_event WHERE biz_key='backup-synthetic-0'")
    status, replay = target.request("POST", "/api/callback/demo/demo",
                                    integration.order("backup-synthetic-0"), signed=True)
    if status != 200 or not replay.get("duplicate") or replay.get("eventId") != original:
        raise AssertionError("恢复后的订单身份或幂等记录不一致")
    verify_restored_writes(target)
    return {"tables": before, "dumpBytes": len(dump), "backupSeconds": round(backup_seconds, 3),
            "restoreSeconds": round(restore_seconds, 3), "restoredOrderStillDuplicate": True,
            "wrongDatadirRejected": True, "restoredWritesAndCountersVerified": True}


def main():
    """解析命令行并执行隔离演练；返回 0 表示通过，1 表示演练或清理失败。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--mysql-bin", type=Path, required=True)
    parser.add_argument("--dump-tools", type=Path,
                        help="可选的新版 mysqldump 目录，用于旧服务器配套工具缺少 --init-command 的情况")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/backup-drill-result.json")
    args = parser.parse_args()
    source, target = (integration.Fixture(args.binary, args.mysql_bin) for _ in range(2))
    failure = None
    report = {"formatVersion": 1, "capturedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "note": "本次只使用两套新建临时实例和合成数据，不代表生产备份策略验收。"}
    try:
        report.update(execute(source, target, args.dump_tools), passed=True)
    except BaseException as error:
        failure = error
        report.update(passed=False, failureType=type(error).__name__)
    finally:
        for fixture in (target, source):
            try:
                fixture.close(failure=failure)
            except Exception as error:
                report.update(passed=False, cleanupFailureType=type(error).__name__)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("备份恢复演练" + ("通过" if report["passed"] else "失败") + "，结果：" + str(args.output.resolve()))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

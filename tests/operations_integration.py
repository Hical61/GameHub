"""在临时 MySQL 中检查统计、归档、载荷清理和监控接口，不接已有数据库。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import statistics
import threading
import uuid

from integration import Fixture, order, run_fixture, wait_for
import integration


class UpgradeFixture(Fixture):
    """覆盖基础初始化、归档统计和多游戏统计阶段的存量升级及完整迁移的首错拒绝。"""

    migration_boundary = "-- 多游戏计数：建立按游戏划分的计数分片与索引。"
    observability_boundary = "-- 投递观测与运行控制：记录投递尝试、配置回执和游戏运行控制。"

    def sql(self, sql, timeout=10):
        """拦截本次建库和合并迁移；返回 SQL 结果，其余操作沿用隔离实例保护。"""
        if "CREATE TABLE schema_version" in sql:
            result = super().sql(sql, timeout=timeout)
            self.seed_v1("gamehub")
            return result
        if self.migration_boundary not in sql:
            return super().sql(sql, timeout=timeout)
        self.verify_legacy_upgrade(sql, 1)
        self.verify_legacy_upgrade(sql, 3)
        # 先执行归档统计阶段，构造有真实全局计数触发器和归档数据的合成库。
        legacy_stage = sql.split(self.migration_boundary, 1)[0]
        super().sql(legacy_stage, timeout=timeout)
        self.archive_seed("gamehub")
        assert super().sql("SELECT MAX(version) FROM gamehub.schema_version") == "2"
        before = self.task_counts("gamehub", "delivery_task_counts")
        assert before == "1\t1\t1\t1\t1", "归档统计测试库未包含四种活动状态及归档数据"
        history = self.history_snapshot("gamehub", 2)
        result = super().sql(sql, timeout=timeout)
        self.verify_upgrade("gamehub", before, history, sql)
        print("通过：归档统计阶段执行完整升级，四状态、归档、计数及历史内容保留")
        return result

    def verify_upgrade(self, database, counts, history, migration):
        """核对指定库升级后的历史、计数、运行维护表和触发器，并验证完整迁移重复拒绝。"""
        assert super().sql(f"SELECT MAX(version) FROM {database}.schema_version") == "4"
        versions = super().sql(f"SELECT version FROM {database}.schema_version ORDER BY version")
        assert versions.splitlines() == ["1", "2", "3", "4"], "合并迁移没有保留完整版本记录"
        current = self.history_snapshot(database, 4)
        assert all(current[name] == value for name, value in history.items()), "合并迁移修改了历史内容或已有计数分片"
        assert self.task_counts(database, "delivery_task_counts") == counts, "全局计数未保留"
        assert self.task_counts(database, "game_delivery_task_counts") == counts, "游戏计数未正确保留或回填"
        versions = super().sql("SELECT COUNT(*) FROM " + database + ".hub_event "
                               "WHERE payload_version<>1 OR fingerprint_version<>1")
        assert versions == "0", "合并迁移修改了历史载荷或摘要的默认版本"
        for table in ("delivery_attempt", "config_server_state", "game_delivery_control"):
            assert super().sql(f"SELECT COUNT(*) FROM {database}.{table}") == "0", "升级伪造了已有任务的运行历史"
        self.verify_trigger_order(database)
        self.verify_repeat_rejected(database, migration)

    def seed_v1(self, database):
        """插入合成历史和五条任务，四状态齐全并多留一条 done 供归档测试。"""
        super().sql(f"INSERT INTO {database}.hub_event(game_id,task_type,namespace_key,biz_key,request_hash,payload,created_at) "
                "VALUES('demo','announce','migration','upgrade-seed',REPEAT('0',64),"
                "'{\"note\":\"upgrade-history\",\"amountMinor\":1234,\"nested\":{\"keep\":true}}',UTC_TIMESTAMP(6)); "
                f"SET @upgrade_event=LAST_INSERT_ID(); INSERT INTO {database}.delivery_task"
                "(event_id,game_id,server_id,status,next_retry_at,lease_until,created_at,updated_at) "
                "SELECT @upgrade_event,'demo',900000+s.n,IF(s.n=4,2,s.n),TIMESTAMPADD(DAY,1,UTC_TIMESTAMP(6)),"
                "TIMESTAMPADD(DAY,1,UTC_TIMESTAMP(6)),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6) "
                "FROM (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4) s")

    def archive_seed(self, database):
        """在具备归档统计结构的合成库将额外的 done 移入归档，保留四种活动状态和真实计数。"""
        super().sql(f"USE {database}; START TRANSACTION; INSERT INTO delivery_task_archive "
                "SELECT t.*,UTC_TIMESTAMP(6) FROM delivery_task t JOIN hub_event e ON e.id=t.event_id "
                "WHERE e.biz_key='upgrade-seed' AND t.server_id=900004 AND t.status=2; "
                "DELETE t FROM delivery_task t JOIN hub_event e ON e.id=t.event_id "
                "WHERE e.biz_key='upgrade-seed' AND t.server_id=900004 AND t.status=2; COMMIT")

    def task_counts(self, database, table):
        """返回测试计数表的五种汇总，仅供迁移前后比较。"""
        return super().sql("SELECT SUM(pending_count),SUM(delivering_count),SUM(done_count),"
                           f"SUM(failed_count),SUM(archived_count) FROM {database}.{table}")

    def history_snapshot(self, database, version):
        """返回合成事件和任务的完整基础字段；基础初始化不含归档表，按空归档比较。"""
        events = super().sql("SELECT id,game_id,task_type,namespace_key,biz_key,request_hash,"
                             f"CAST(payload AS CHAR),created_at FROM {database}.hub_event ORDER BY id")
        tasks = super().sql(f"SELECT * FROM {database}.delivery_task ORDER BY id")
        archive = super().sql(f"SELECT * FROM {database}.delivery_task_archive ORDER BY id") if version >= 2 else ""
        redaction_column = "payload_redacted_at" if version >= 2 else "NULL"
        redactions = super().sql(f"SELECT id,{redaction_column} FROM {database}.hub_event ORDER BY id")
        history = {"events": events, "tasks": tasks, "archive": archive, "redactions": redactions}
        if version >= 2:
            history["globalCounts"] = super().sql(f"SELECT * FROM {database}.delivery_task_counts ORDER BY bucket_id")
        if version >= 3:
            history["gameCounts"] = super().sql(f"SELECT * FROM {database}.game_delivery_task_counts ORDER BY game_id,bucket_id")
            history["versions"] = super().sql(f"SELECT id,payload_version,fingerprint_version FROM {database}.hub_event ORDER BY id")
        return history

    def verify_legacy_upgrade(self, migration, version):
        """从职责阶段构造辅助库，再直接执行整份运行维护迁移。"""
        database = f"gamehub_from_v{version}"
        initial = (integration.ROOT / "migrations/001_initial.sql").read_text(encoding="utf-8")
        super().sql(f"CREATE DATABASE {database} CHARACTER SET utf8mb4; USE {database};\n" + initial)
        self.seed_v1(database)
        migration = migration.replace("USE gamehub;", f"USE {database};", 1)
        counts = "1\t1\t2\t1\t0"
        if version == 3:
            assert self.observability_boundary in migration, "合并迁移缺少投递观测与运行控制的阶段边界"
            super().sql(migration.split(self.observability_boundary, 1)[0])
            self.archive_seed(database)
            counts = "1\t1\t1\t1\t1"
            assert self.task_counts(database, "delivery_task_counts") == counts
            assert self.task_counts(database, "game_delivery_task_counts") == counts
        assert super().sql(f"SELECT MAX(version) FROM {database}.schema_version") == str(version)
        history = self.history_snapshot(database, version)
        super().sql(migration)
        self.verify_upgrade(database, counts, history, migration)
        if version == 3:
            self.verify_appended_stage(database, migration)
        super().sql(f"DROP DATABASE {database}")
        stage = "基础初始化" if version == 1 else "多游戏统计阶段"
        print(f"通过：{stage}执行完整升级，历史及计数保留，投递观测与运行控制三表为空")

    def verify_appended_stage(self, database, migration):
        """只在辅助库模拟追加迁移，确认已完成阶段允许后续结构变更继续执行。"""
        target_setting = "SET @gamehub_target_version = 4;"
        assert migration.count(target_setting) == 1, "累计迁移目标版本必须由唯一入口设置"
        future = migration.replace(target_setting, "SET @gamehub_target_version = 5;", 1)
        future += "\nSET @gamehub_source_version = (SELECT MAX(version) FROM schema_version);\n"
        future += ("SET @gamehub_migration_sql = IF(@gamehub_source_version=4,"
                   "'CREATE TABLE synthetic_upgrade_append (marker INT NOT NULL PRIMARY KEY) ENGINE=InnoDB','DO 0');\n"
                   "PREPARE gamehub_migration_step FROM @gamehub_migration_sql;\n"
                   "EXECUTE gamehub_migration_step;\nDEALLOCATE PREPARE gamehub_migration_step;\n"
                   "INSERT INTO schema_version(version,installed_at) "
                   "SELECT 5,UTC_TIMESTAMP(6) WHERE @gamehub_source_version=4;\n")
        tables = super().sql("SELECT TABLE_NAME FROM information_schema.TABLES "
                             f"WHERE TABLE_SCHEMA='{database}' AND TABLE_NAME<>'schema_version' ORDER BY TABLE_NAME").splitlines()
        before = self.table_checksums(database, tables)
        versions = super().sql(f"SELECT * FROM {database}.schema_version ORDER BY version")
        super().sql(future)
        assert super().sql(f"SELECT MAX(version) FROM {database}.schema_version") == "5"
        assert super().sql(f"SELECT * FROM {database}.schema_version WHERE version<=4 ORDER BY version") == versions
        assert self.table_checksums(database, tables) == before, "追加阶段修改了原有业务内容或计数"
        assert super().sql(f"SELECT COUNT(*) FROM {database}.synthetic_upgrade_append") == "0"
        self.verify_trigger_order(database)
        self.verify_repeat_rejected(database, future, version=5)
        print("通过：仅辅助库模拟追加迁移，已有阶段跳过、历史保留且重复执行首错拒绝")

    def verify_trigger_order(self, database):
        """逐对确认游戏计数触发器始终在对应全局触发器之后执行。"""
        rows = super().sql("SELECT TRIGGER_NAME,ACTION_ORDER FROM information_schema.TRIGGERS "
                            f"WHERE TRIGGER_SCHEMA='{database}' ORDER BY TRIGGER_NAME")
        orders = {name: int(order) for name, order in (row.split("\t") for row in rows.splitlines())}
        for name in ("task_count_insert", "task_count_update", "task_count_delete",
                     "archive_count_insert", "archive_count_delete"):
            assert orders["hub_" + name] < orders["hub_game_" + name], "全局和游戏计数触发器执行顺序颠倒"

    def snapshot_schema(self, database):
        """返回表内容校验和、完整建表定义和触发器定义，仅在内存中比较。"""
        tables = super().sql("SELECT TABLE_NAME FROM information_schema.TABLES "
                             f"WHERE TABLE_SCHEMA='{database}' ORDER BY TABLE_NAME").splitlines()
        checksum = self.table_checksums(database, tables)
        definitions = super().sql("; ".join(f"SHOW CREATE TABLE {database}.{table}" for table in tables))
        triggers = super().sql("SELECT TRIGGER_NAME,EVENT_MANIPULATION,ACTION_TIMING,ACTION_ORDER,ACTION_STATEMENT "
                               "FROM information_schema.TRIGGERS "
                               f"WHERE TRIGGER_SCHEMA='{database}' ORDER BY TRIGGER_NAME")
        return checksum, definitions, triggers

    def table_checksums(self, database, tables):
        """计算指定隔离表的内容校验和，返回文本；任一表无法校验时立即失败。"""
        checksum = super().sql("CHECKSUM TABLE " + ",".join(f"{database}.{table}" for table in tables))
        assert all(re.fullmatch(r"[^\t]+\t\d+", line) for line in checksum.splitlines()), "表内容校验和不可用"
        return checksum

    def verify_repeat_rejected(self, database, migration, version=4):
        """已完成版本再次执行须在 DDL 前拒绝，保留所有表内容及触发器。"""
        before = self.snapshot_schema(database)
        try:
            super().sql(migration)
        except RuntimeError:
            error = self.sql_errors[-1]
            # Fixture.sql 在输入前添加一行实例身份检查；错误必须来自首个版本守卫。
            guard_line = migration[:migration.index("PREPARE gamehub_migration_guard")].count("\n") + 2
            assert error["code"] == 1064 and error["line"] == guard_line, f"版本 {version} 没有在首个版本守卫拒绝"
        else:
            raise AssertionError(f"版本 {version} 未拒绝重复执行合并迁移")
        assert self.snapshot_schema(database) == before, "拒绝重复迁移时修改了已完成的库"


def counts_match(fixture):
    """比较事务计数与实际表内容，覆盖完成、归档和回滚后的变化。"""
    rows = fixture.sql("SELECT status,COUNT(*) FROM gamehub.delivery_task GROUP BY status")
    actual = dict.fromkeys(("pending", "delivering", "done", "failed"), 0)
    names = tuple(actual)
    for line in rows.splitlines():
        state, count = map(int, line.split("\t"))
        actual[names[state]] = count
    status, report = fixture.request("GET", "/admin/stats", admin=fixture.admin())
    assert status == 200
    assert all(report[key] == value for key, value in actual.items()), "事务计数与任务表不一致"
    archived = int(fixture.sql("SELECT COUNT(*) FROM gamehub.delivery_task_archive"))
    assert report["archivedDone"] == archived
    assert report["completedTotal"] == actual["done"] + archived
    game_status, game_report = fixture.request("GET", "/admin/stats/demo", admin=fixture.admin())
    assert game_status == 200
    # 存储回归包含其他游戏的计数样本，游戏接口必须和同一范围的实际任务比较。
    game_actual = dict.fromkeys(names, 0)
    for line in fixture.sql("SELECT status,COUNT(*) FROM gamehub.delivery_task WHERE game_id='demo' GROUP BY status").splitlines():
        state, count = map(int, line.split("\t"))
        game_actual[names[state]] = count
    game_archived = int(fixture.sql("SELECT COUNT(*) FROM gamehub.delivery_task_archive WHERE game_id='demo'"))
    assert all(game_report[key] == value for key, value in game_actual.items()), "按游戏事务计数与升级后任务不一致"
    assert game_report["archivedDone"] == game_archived


def archive(fixture, batch=1, redact=False):
    """发起一批人工归档，返回受限的处理数量。"""
    return fixture.request("POST", "/admin/archive/demo", {
        "keepDays": 1, "batchSize": batch, "redactPayload": redact}, admin=fixture.admin())


def age_event(fixture, event_id):
    """只把本次合成事件和任务改成三天前，便于验证保留期。"""
    event_id = int(event_id)
    fixture.sql(f"UPDATE gamehub.hub_event SET created_at=TIMESTAMPADD(DAY,-3,UTC_TIMESTAMP(6)) WHERE id={event_id}; "
                f"UPDATE gamehub.delivery_task SET updated_at=TIMESTAMPADD(DAY,-3,UTC_TIMESTAMP(6)) WHERE event_id={event_id}")


def verify_archive(fixture):
    """验证归档故障回滚、长期幂等、广播剩余目标和可重跑性。"""
    status, accepted = fixture.request("POST", "/api/callback/demo/demo", order("ops-archive"), signed=True)
    assert status == 200
    event_id = accepted["eventId"]
    wait_for(lambda: fixture.tasks(event_id) == [2], "归档测试订单投递完成")
    age_event(fixture, event_id)
    fixture.sql("USE gamehub; CREATE TRIGGER synthetic_archive_failure BEFORE INSERT ON delivery_task_archive "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic archive failure'")
    assert archive(fixture)[0] == 503
    assert fixture.tasks(event_id) == [2]
    assert fixture.sql(f"SELECT COUNT(*) FROM gamehub.delivery_task_archive WHERE event_id={int(event_id)}") == "0"
    fixture.sql("DROP TRIGGER gamehub.synthetic_archive_failure")
    counts_match(fixture)
    status, result = archive(fixture)
    assert status == 200 and result == {"archivedTasks": 1, "redactedEvents": 0}
    assert fixture.tasks(event_id) == []
    assert fixture.sql(f"SELECT payload_redacted_at IS NULL FROM gamehub.hub_event WHERE id={int(event_id)}") == "1"
    assert archive(fixture, redact=True) == (200, {"archivedTasks": 0, "redactedEvents": 1})
    assert fixture.sql(f"SELECT JSON_LENGTH(payload) FROM gamehub.hub_event WHERE id={int(event_id)}") == "0"
    assert archive(fixture, redact=True) == (200, {"archivedTasks": 0, "redactedEvents": 0})
    status, duplicate = fixture.request("POST", "/api/callback/demo/demo", order("ops-archive"), signed=True)
    assert status == 200 and duplicate["duplicate"] and duplicate["eventId"] == event_id
    changed = order("ops-archive")
    changed["amountMinor"] += 1
    assert fixture.request("POST", "/api/callback/demo/demo", changed, signed=True)[0] == 409
    assert fixture.ledger(1, "ops-archive") == 1 and fixture.tasks(event_id) == []
    status, history = fixture.request("GET", "/admin/archive/demo/0", admin=fixture.admin("viewer", ["demo"]))
    assert status == 200 and any(item["eventId"] == event_id for item in history["items"])
    print("通过：归档事务回滚、载荷清理、长期判重、同键冲突和归档查询")

    fixture.receivers[1].fail = True
    broadcast_body = {"bizKey": "ops-broadcast", "serverId": 0, "content": "归档广播测试"}
    status, accepted = fixture.request("POST", "/admin/announce/demo", broadcast_body, admin=fixture.admin())
    assert status == 200
    broadcast = accepted["eventId"]
    wait_for(lambda: fixture.tasks(broadcast) == [2, 3], "广播部分失败后归档")
    age_event(fixture, broadcast)
    assert archive(fixture, redact=True) == (200, {"archivedTasks": 1, "redactedEvents": 0})
    assert fixture.sql(f"SELECT payload_redacted_at IS NULL FROM gamehub.hub_event WHERE id={int(broadcast)}") == "1"
    failed_task = fixture.sql(f"SELECT id FROM gamehub.delivery_task WHERE event_id={int(broadcast)}")
    fixture.receivers[1].fail = False
    assert fixture.request("POST", f"/admin/tasks/demo/{failed_task}/replay", admin=fixture.admin())[0] == 200
    wait_for(lambda: fixture.tasks(broadcast) == [2], "归档后剩余广播任务重排完成")
    age_event(fixture, broadcast)
    assert archive(fixture, redact=True) == (200, {"archivedTasks": 1, "redactedEvents": 1})
    assert fixture.request("POST", "/admin/announce/demo", broadcast_body, admin=fixture.admin())[1]["duplicate"]
    assert fixture.tasks(broadcast) == []
    counts_match(fixture)
    print("通过：只归档 done，失败目标仍可重排，全部归档后才清理广播载荷")


def verify_permissions(fixture):
    """检查监控的全局授权和归档写权限，不能从新接口绕过原有授权。"""
    path = "/admin/metrics"
    assert fixture.request("GET", path)[0] == 401
    assert fixture.request("GET", path, admin=fixture.admin("viewer", ["demo"]))[0] == 403
    status, metrics = fixture.request("GET", path, admin=fixture.admin("viewer"))
    assert status == 200 and 'gamehub_tasks{state="pending"}' in metrics["message"]
    body = {"keepDays": 1, "batchSize": 1, "redactPayload": True}
    assert fixture.request("POST", "/admin/archive/demo", body, admin=fixture.admin("viewer"))[0] == 403
    assert fixture.request("POST", "/admin/archive/demo", body, admin=fixture.admin(games=["other"]))[0] == 403
    assert fixture.request("POST", "/admin/archive/demo", {**body, "keepDays": 0}, admin=fixture.admin())[0] == 400
    assert fixture.request("POST", "/admin/archive/demo", {**body, "batchSize": 1001}, admin=fixture.admin())[0] == 400
    counts_match(fixture)
    fixture.sql("START TRANSACTION; INSERT INTO gamehub.delivery_task"
                "(event_id,game_id,server_id,status,next_retry_at,created_at,updated_at) "
                "SELECT id,'demo',999999,3,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6) "
                "FROM gamehub.hub_event WHERE biz_key='upgrade-seed'; ROLLBACK")
    counts_match(fixture)
    print("通过：指标全局授权、归档 operator 权限及参数边界")


def verify_parallel(fixture):
    """两次同时归档不能复制同一任务；计数必须和表内容一致。"""
    events = []
    for index in range(4):
        status, result = fixture.request("POST", "/api/callback/demo/demo", order(f"ops-parallel-{index}"), signed=True)
        assert status == 200
        events.append(result["eventId"])
    wait_for(lambda: all(fixture.tasks(item) == [2] for item in events), "并发归档测试订单完成")
    for event_id in events:
        age_event(fixture, event_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: archive(fixture, batch=2), range(2)))
    assert all(status == 200 for status, _ in results), results
    assert sum(result["archivedTasks"] for _, result in results) == 4
    assert all(fixture.tasks(item) == [] for item in events)
    counts_match(fixture)
    assert archive(fixture, batch=2, redact=True)[1]["redactedEvents"] <= 2
    print("通过：并发归档批次不重复、单批数量有界、统计与实际任务一致")


def create_trigger(fixture, name, body):
    """仅在本次临时库创建归档触发器，支持多条合成故障语句。"""
    fixture.sql("USE gamehub;\nDELIMITER $$\nCREATE TRIGGER " + name +
                " BEFORE INSERT ON delivery_task_archive FOR EACH ROW BEGIN " + body +
                " END$$\nDELIMITER ;")


def verify_retry_boundaries(fixture):
    """持续 1213 最多三次，其他错误只执行一次；失败后不留下半批归档。"""
    ids = []
    for index in range(2):
        status, accepted = fixture.request("POST", "/api/callback/demo/demo", order(f"ops-retry-limit-{index}"), signed=True)
        assert status == 200
        ids.append(accepted["eventId"])
    wait_for(lambda: all(fixture.tasks(event) == [2] for event in ids), "重试边界测试订单完成")
    for event in ids:
        age_event(fixture, event)
    selected = fixture.sql("SELECT id FROM gamehub.delivery_task WHERE event_id IN (" + ",".join(map(str, ids)) + ") ORDER BY id")
    second_task = int(selected.splitlines()[-1])
    # MyISAM 只用作测试尝试计数，确保事务回滚后仍能核对实际次数。
    fixture.sql("CREATE TABLE gamehub.synthetic_retry_attempts(marker INT NOT NULL) ENGINE=MyISAM")
    for code, sqlstate, attempts in ((1213, "40001", 3), (1644, "45000", 1)):
        create_trigger(fixture, "synthetic_retry_failure",
                       f"IF NEW.id={second_task} THEN INSERT INTO synthetic_retry_attempts VALUES(1); "
                       f"SIGNAL SQLSTATE '{sqlstate}' SET MYSQL_ERRNO={code},MESSAGE_TEXT='synthetic retry boundary'; END IF;")
        assert archive(fixture, batch=2)[0] == 503
        assert int(fixture.sql("SELECT COUNT(*) FROM gamehub.synthetic_retry_attempts")) == attempts
        assert all(fixture.tasks(event) == [2] for event in ids), "归档失败遗失了活动任务"
        assert fixture.sql("SELECT COUNT(*) FROM gamehub.delivery_task_archive WHERE event_id IN (" +
                           ",".join(map(str, ids)) + ")") == "0", "归档失败留下了半批历史"
        counts_match(fixture)
        fixture.sql("DROP TRIGGER gamehub.synthetic_retry_failure; DELETE FROM gamehub.synthetic_retry_attempts")
    fixture.sql("DROP TABLE gamehub.synthetic_retry_attempts")
    assert archive(fixture, batch=2) == (200, {"archivedTasks": 2, "redactedEvents": 0})
    counts_match(fixture)
    print("通过：1213 整笔最多三次，非 1213 不重试，失败批次与计数完整回滚")


def run_gated_archive_pair(fixture, first_ids):
    """让第一批实际等待第二批的计数分片，再放行第二批形成真实 InnoDB 死锁。"""
    gate = "ops-release-" + uuid.uuid4().hex[:12]
    signal = "ops-arrived-" + uuid.uuid4().hex[:12] + "-"
    trigger = (f"IF NEW.id IN ({','.join(map(str, first_ids))}) THEN "
               f"DO GET_LOCK(CONCAT('{signal}',NEW.id),0); DO GET_LOCK(CONCAT('{gate}-',NEW.id),4); "
               f"DO RELEASE_LOCK(CONCAT('{gate}-',NEW.id)); DO RELEASE_LOCK(CONCAT('{signal}',NEW.id)); END IF;")
    # 显式排在两套计数之后。独立时间闸只控制调度，不增加业务事务之间的锁依赖。
    fixture.sql("USE gamehub;\nDELIMITER $$\nCREATE TRIGGER synthetic_counter_schedule "
                "AFTER INSERT ON delivery_task_archive FOR EACH ROW FOLLOWS hub_game_archive_count_insert BEGIN " +
                trigger + " END$$\nDELIMITER ;")
    holder_ids = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        holders = [pool.submit(fixture.sql, f"SELECT GET_LOCK('{gate}-{task}',0); DO SLEEP(20)", 25)
                   for task in first_ids]
        try:
            def wait_for_owners(prefix, description, timeout=20):
                # 一条 SELECT 同时观测两笔，避免逐连接轮询时第一个闸已超时、第二个才到达。
                owners = []
                def ready():
                    nonlocal owners
                    owners = fixture.sql("SELECT " + ",".join(f"IS_USED_LOCK('{prefix}{task}')" for task in first_ids)).split("\t")
                    return "NULL" not in owners
                wait_for(ready, description, timeout)
                return [int(owner) for owner in owners]

            holder_ids = wait_for_owners(gate + "-", "归档调度闸就绪")
            pending = [pool.submit(archive, fixture, 2) for _ in range(2)]
            first_worker = wait_for_owners(signal, "两批归档分别持有计数分片", 3)[0]
            # ID 只来自本次实例独有的用户锁。先只放行第一批，证明它已等待第二批持有的分片。
            fixture.sql(f"KILL {holder_ids.pop(0)}")
            wait_for(lambda: fixture.sql("SELECT COUNT(*) FROM information_schema.INNODB_TRX "
                     f"WHERE trx_mysql_thread_id={first_worker} AND trx_state='LOCK WAIT'") == "1",
                     "第一批归档等待另一批计数分片", 1)
            fixture.sql(f"KILL {holder_ids.pop(0)}")
            for holder in holders:
                try:
                    holder.result(timeout=5)
                except RuntimeError:
                    pass
            return [future.result(timeout=10) for future in pending]
        finally:
            for holder_id in holder_ids:
                fixture.sql(f"KILL {holder_id}")
            fixture.sql("DROP TRIGGER gamehub.synthetic_counter_schedule")


def latest_deadlock(fixture):
    """只在内存中读取最新引擎死锁图，不保存或输出其中的 SQL。"""
    status = fixture.sql("SHOW ENGINE INNODB STATUS").replace("\\n", "\n")
    _, heading, report = status.partition("LATEST DETECTED DEADLOCK")
    return re.split(r"\n-+\nTRANSACTIONS\n", report, maxsplit=1)[0] if heading else ""


def archive_retries_since(fixture, offsets):
    """只数本轮日志偏移后的固定 1213 重试记录，排除前面的 SIGNAL 次数测试。"""
    pattern = "数据库死锁重试，action=archive attempt=\\d+ category=mysql\\.common-server code=1213".encode("utf-8")
    return sum(len(re.findall(pattern, log.read_bytes()[offsets.get(log, 0):]))
               for log in fixture.path.glob("hub-*.log"))


def verify_counter_deadlock_retry(fixture):
    """复现 63→0 与 0→63 的反序计数锁，重试后全部完成；再用原触发器复测。"""
    highest = int(fixture.sql("SELECT GREATEST(COALESCE((SELECT MAX(id) FROM gamehub.delivery_task),0),"
                              "COALESCE((SELECT MAX(id) FROM gamehub.delivery_task_archive),0))"))
    base = (highest // 64 + 1) * 64
    task_ids = (base + 63, base + 64, base + 128, base + 191)
    fixture.sql("INSERT INTO gamehub.hub_event(game_id,task_type,namespace_key,biz_key,request_hash,payload,created_at) "
                "VALUES('demo','announce','ops','counter-lock-order',REPEAT('0',64),'{}',TIMESTAMPADD(DAY,-4,UTC_TIMESTAMP(6)))")
    event = int(fixture.sql("SELECT id FROM gamehub.hub_event WHERE namespace_key='ops' AND biz_key='counter-lock-order'"))
    fixture.sql("INSERT INTO gamehub.delivery_task(id,event_id,game_id,server_id,status,next_retry_at,created_at,updated_at) VALUES " +
                ",".join(f"({task},{event},'demo',{900000+index},2,UTC_TIMESTAMP(6),TIMESTAMPADD(DAY,-4,UTC_TIMESTAMP(6)),"
                         f"TIMESTAMPADD(DAY,-{3 if index<2 else 2},UTC_TIMESTAMP(6)))" for index, task in enumerate(task_ids)))
    fixture.sql("SET GLOBAL innodb_monitor_enable='lock_deadlocks'")
    def deadlocks():
        return int(fixture.sql("SELECT COUNT FROM information_schema.INNODB_METRICS WHERE NAME='lock_deadlocks'"))
    before = deadlocks()
    previous_report = latest_deadlock(fixture)
    offsets = {log: log.stat().st_size for log in fixture.path.glob("hub-*.log")}
    responses = run_gated_archive_pair(fixture, (task_ids[0], task_ids[2]))
    assert all(status == 200 for status, _ in responses), responses
    assert sum(result["archivedTasks"] for _, result in responses) == 4
    metric_delta = deadlocks() - before
    report = latest_deadlock(fixture)
    assert report and report != previous_report and "delivery_task_counts" in report, "引擎没有本轮计数分片死锁图"
    wait_for(lambda: archive_retries_since(fixture, offsets) >= 1, "本轮实际归档死锁的重试日志")
    controlled = archive_retries_since(fixture, offsets)
    # 8.0.16 的 lock_deadlocks 会漏计，官方 Bug 77399 在 8.0.18 才修复。
    # 因此用新引擎锁图和本轮真实 1213 双重确认，计数器增量单独报告，不把零当成没死锁。
    assert controlled >= 1, "未形成预期的 InnoDB 计数分片死锁"
    keys = ",".join(map(str, task_ids))
    for _ in range(10):
        fixture.sql("START TRANSACTION; INSERT INTO gamehub.delivery_task "
                    "SELECT id,event_id,game_id,server_id,status,attempts,generation,next_retry_at,lease_until,last_error,created_at,updated_at "
                    f"FROM gamehub.delivery_task_archive WHERE id IN ({keys}); "
                    f"DELETE FROM gamehub.delivery_task_archive WHERE id IN ({keys}); COMMIT")
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: archive(fixture, batch=2), range(2)))
        assert all(status == 200 for status, _ in responses), responses
        assert sum(result["archivedTasks"] for _, result in responses) == 4
    counts_match(fixture)
    print(f"通过：计数锁反序实际 1213 重试 {controlled} 次，metricDelta={metric_delta}，"
          "新引擎死锁图已确认；原触发器十轮并发归档通过")


def benchmark_stats(fixture, rows, output):
    """在同一批合成历史上比较全扫描与事务计数 SQL，写入条件和原始耗时。"""
    fixture._stop_child(fixture.hub)
    fixture.sql("INSERT INTO gamehub.hub_event(game_id,task_type,namespace_key,biz_key,request_hash,payload,created_at) "
                "VALUES('demo','announce','benchmark','stats-seed',REPEAT('0',64),'{}',UTC_TIMESTAMP(6))")
    event_id = int(fixture.sql("SELECT id FROM gamehub.hub_event WHERE namespace_key='benchmark' AND biz_key='stats-seed'"))
    digits = "(" + " UNION ALL ".join(f"SELECT {number} n" for number in range(10)) + ")"
    fixture.sql("INSERT INTO gamehub.delivery_task(event_id,game_id,server_id,status,next_retry_at,created_at,updated_at) "
        f"SELECT {event_id},'demo',1+a.n+10*b.n+100*c.n+1000*d.n+10000*e.n,2,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),"
        f"UTC_TIMESTAMP(6) FROM {digits} a CROSS JOIN {digits} b CROSS JOIN {digits} c CROSS JOIN {digits} d "
        f"CROSS JOIN {digits} e LIMIT {rows}", timeout=120)
    original = ("SELECT status,COUNT(*),COALESCE(MAX(TIMESTAMPDIFF(SECOND,created_at,UTC_TIMESTAMP(6))),0) "
                "FROM gamehub.delivery_task GROUP BY status")
    current = ("SELECT CAST(SUM(pending_count) AS UNSIGNED),CAST(SUM(delivering_count) AS UNSIGNED),"
               "CAST(SUM(done_count) AS UNSIGNED),CAST(SUM(failed_count) AS UNSIGNED),CAST(SUM(archived_count) AS UNSIGNED),"
               "(SELECT GREATEST(0,COALESCE(TIMESTAMPDIFF(SECOND,MIN(created_at),UTC_TIMESTAMP(6)),0)) "
               "FROM gamehub.delivery_task FORCE INDEX(idx_status_created) WHERE status=0) FROM gamehub.delivery_task_counts")
    report = {"historyRows": rows, "mysqlVersion": fixture.sql("SELECT VERSION()"), "bufferPoolMiB": 32, "queries": {}}
    for name, query in (("original", original), ("current", current)):
        samples = []
        for _ in range(9):
            value = fixture.sql("SET @benchmark_start=UTC_TIMESTAMP(6); " + query +
                                "; SELECT TIMESTAMPDIFF(MICROSECOND,@benchmark_start,UTC_TIMESTAMP(6))")
            samples.append(int(value.splitlines()[-1]))
        report["queries"][name] = {"samplesMicroseconds": samples, "medianMicroseconds": statistics.median(samples)}
    report["currentPlan"] = fixture.sql("EXPLAIN " + current)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("统计对照结果：" + json.dumps({name: result["medianMicroseconds"] for name, result in report["queries"].items()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--mysql-bin", type=Path, required=True)
    parser.add_argument("--history-rows", type=int, choices=(0, 10000, 100000), default=0)
    parser.add_argument("--output", type=Path, default=integration.ROOT / ".local/statistics-benchmark.json")
    args = parser.parse_args()
    fixture = UpgradeFixture(args.binary, args.mysql_bin)
    def verify(current):
        verify_permissions(current)
        verify_archive(current)
        verify_parallel(current)
        verify_retry_boundaries(current)
        verify_counter_deadlock_retry(current)
        if args.history_rows:
            benchmark_stats(current, args.history_rows, args.output)
    run_fixture(fixture, verifier=verify)


if __name__ == "__main__":
    main()

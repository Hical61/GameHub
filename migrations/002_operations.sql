-- 运行维护结构迁移：停止所有 Hub 和其他写入并备份后执行。
-- 按结构记录补齐归档统计、多游戏计数、投递观测与运行控制所需对象。
-- 使用遇错立即停止的非交互客户端，逐文件检查退出码；不要使用交互 SOURCE 或 --force。
-- 已完成迁移会在任何 DDL 前拒绝重复执行；DDL 失败时先检查对象，不能盲目重跑。
SET @gamehub_target_version = 4;
SET @gamehub_source_version = (SELECT MAX(version) FROM schema_version);
SET @gamehub_migration_sql = IF(@gamehub_source_version>=1 AND @gamehub_source_version<@gamehub_target_version,
    'DO 0', 'INVALID GAMEHUB SCHEMA VERSION');
PREPARE gamehub_migration_guard FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_guard;
DEALLOCATE PREPARE gamehub_migration_guard;

-- 归档统计：已有的归档表、全局计数和相关字段保持原样。
-- 归档统计对象仅在基础初始化后创建；原业务键、载荷及摘要不会被重写。
SET @gamehub_migration_sql = IF(@gamehub_source_version=1,
    'ALTER TABLE delivery_task
    ADD KEY idx_status_created (status, created_at, id),
    ADD KEY idx_game_archive (game_id, status, updated_at, id)', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

SET @gamehub_migration_sql = IF(@gamehub_source_version=1,
    'ALTER TABLE hub_event
    ADD COLUMN payload_redacted_at DATETIME(6) NULL,
    ADD KEY idx_game_redaction (game_id, payload_redacted_at, created_at, id)', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

SET @gamehub_migration_sql = IF(@gamehub_source_version=1,
    'CREATE TABLE delivery_task_archive (
    id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    event_id BIGINT UNSIGNED NOT NULL,
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    server_id INT UNSIGNED NOT NULL,
    status TINYINT UNSIGNED NOT NULL,
    attempts INT UNSIGNED NOT NULL,
    generation BIGINT UNSIGNED NOT NULL,
    next_retry_at DATETIME(6) NOT NULL,
    lease_until DATETIME(6) NULL,
    last_error VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    archived_at DATETIME(6) NOT NULL,
    UNIQUE KEY uk_archive_target (event_id, server_id),
    KEY idx_archive_game (game_id, id),
    CONSTRAINT fk_archive_event FOREIGN KEY (event_id) REFERENCES hub_event(id),
    CONSTRAINT ck_archive_done CHECK (status=2)
) ENGINE=InnoDB', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

SET @gamehub_migration_sql = IF(@gamehub_source_version=1,
    'CREATE TABLE delivery_task_counts (
    bucket_id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    pending_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    delivering_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    done_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    failed_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    archived_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    CONSTRAINT ck_counts_bucket CHECK (bucket_id<64)
) ENGINE=InnoDB', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

-- 全局计数只在创建统计结构时初始化；已有的活动和归档计数保持不变。
INSERT INTO delivery_task_counts(bucket_id)
SELECT a.n+8*b.n
FROM (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3
      UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7) a
CROSS JOIN (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3
      UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7) b
WHERE @gamehub_source_version=1;

UPDATE delivery_task_counts c JOIN (
    SELECT MOD(id,64) bucket_id, SUM(status=0) pending_count, SUM(status=1) delivering_count,
           SUM(status=2) done_count, SUM(status=3) failed_count
    FROM delivery_task GROUP BY MOD(id,64)
) t ON t.bucket_id=c.bucket_id
SET c.pending_count=t.pending_count, c.delivering_count=t.delivering_count,
    c.done_count=t.done_count, c.failed_count=t.failed_count
WHERE @gamehub_source_version=1;

-- 停写期间按原定义重建两套计数触发器，统一恢复全局先于游戏的执行顺序。
-- DEFINER 为本次迁移账号；迁移后仍须保留其 DML 和 TRIGGER 权限。
DELIMITER $$
DROP TRIGGER IF EXISTS hub_task_count_insert$$
CREATE TRIGGER hub_task_count_insert AFTER INSERT ON delivery_task FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET pending_count=pending_count+(NEW.status=0),
        delivering_count=delivering_count+(NEW.status=1), done_count=done_count+(NEW.status=2),
        failed_count=failed_count+(NEW.status=3) WHERE bucket_id=MOD(NEW.id,64);
END$$
DROP TRIGGER IF EXISTS hub_task_count_update$$
CREATE TRIGGER hub_task_count_update AFTER UPDATE ON delivery_task FOR EACH ROW
BEGIN
    IF OLD.status<>NEW.status THEN
        UPDATE delivery_task_counts SET pending_count=pending_count+(NEW.status=0)-(OLD.status=0),
            delivering_count=delivering_count+(NEW.status=1)-(OLD.status=1),
            done_count=done_count+(NEW.status=2)-(OLD.status=2),
            failed_count=failed_count+(NEW.status=3)-(OLD.status=3) WHERE bucket_id=MOD(NEW.id,64);
    END IF;
END$$
DROP TRIGGER IF EXISTS hub_task_count_delete$$
CREATE TRIGGER hub_task_count_delete AFTER DELETE ON delivery_task FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET pending_count=pending_count-(OLD.status=0),
        delivering_count=delivering_count-(OLD.status=1), done_count=done_count-(OLD.status=2),
        failed_count=failed_count-(OLD.status=3) WHERE bucket_id=MOD(OLD.id,64);
END$$
DROP TRIGGER IF EXISTS hub_archive_count_insert$$
CREATE TRIGGER hub_archive_count_insert AFTER INSERT ON delivery_task_archive FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET archived_count=archived_count+1 WHERE bucket_id=MOD(NEW.id,64);
END$$
DROP TRIGGER IF EXISTS hub_archive_count_delete$$
CREATE TRIGGER hub_archive_count_delete AFTER DELETE ON delivery_task_archive FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET archived_count=archived_count-1 WHERE bucket_id=MOD(OLD.id,64);
END$$
DELIMITER ;

INSERT INTO schema_version(version, installed_at)
SELECT 2, UTC_TIMESTAMP(6) WHERE @gamehub_source_version=1;

-- 多游戏计数：建立按游戏划分的计数分片与索引。
SET @gamehub_migration_sql = IF((SELECT MAX(version) FROM schema_version)>=2,
    'DO 0', 'INVALID GAMEHUB SCHEMA VERSION');
PREPARE gamehub_migration_guard FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_guard;
DEALLOCATE PREPARE gamehub_migration_guard;

-- 已有事件沿用原始载荷和摘要格式标记，保留业务键、载荷及已有摘要，不重新规范化历史内容。
SET @gamehub_migration_sql = IF(@gamehub_source_version IN (1,2),
    'ALTER TABLE hub_event
    ADD COLUMN payload_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    ADD COLUMN fingerprint_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    ADD CONSTRAINT ck_event_versions CHECK (payload_version>0 AND fingerprint_version>0)', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

SET @gamehub_migration_sql = IF(@gamehub_source_version IN (1,2),
    'ALTER TABLE delivery_task
    ADD KEY idx_game_pending (game_id, status, next_retry_at, id),
    ADD KEY idx_game_lease (game_id, status, lease_until, id),
    ADD KEY idx_game_status_created (game_id, status, created_at, id)', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

-- 每款游戏最多 64 个惰性建立的计数分片。全局计数继续保留。
SET @gamehub_migration_sql = IF(@gamehub_source_version IN (1,2),
    'CREATE TABLE game_delivery_task_counts (
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    bucket_id TINYINT UNSIGNED NOT NULL,
    pending_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    delivering_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    done_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    failed_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    archived_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, bucket_id),
    CONSTRAINT ck_game_counts_bucket CHECK (bucket_id<64)
) ENGINE=InnoDB', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

INSERT INTO game_delivery_task_counts(game_id,bucket_id,pending_count,delivering_count,done_count,failed_count,archived_count)
SELECT game_id,bucket_id,SUM(pending_count),SUM(delivering_count),SUM(done_count),SUM(failed_count),SUM(archived_count)
FROM (
    SELECT game_id,MOD(id,64) bucket_id,status=0 pending_count,status=1 delivering_count,
           status=2 done_count,status=3 failed_count,0 archived_count FROM delivery_task
    UNION ALL
    SELECT game_id,MOD(id,64),0,0,0,0,1 FROM delivery_task_archive
) existing_tasks WHERE @gamehub_source_version IN (1,2) GROUP BY game_id,bucket_id;

DELIMITER $$
DROP TRIGGER IF EXISTS hub_game_task_count_insert$$
CREATE TRIGGER hub_game_task_count_insert AFTER INSERT ON delivery_task FOR EACH ROW FOLLOWS hub_task_count_insert
BEGIN
    INSERT INTO game_delivery_task_counts(game_id,bucket_id,pending_count,delivering_count,done_count,failed_count)
    VALUES(NEW.game_id,MOD(NEW.id,64),NEW.status=0,NEW.status=1,NEW.status=2,NEW.status=3)
    ON DUPLICATE KEY UPDATE pending_count=pending_count+(NEW.status=0),
        delivering_count=delivering_count+(NEW.status=1),done_count=done_count+(NEW.status=2),
        failed_count=failed_count+(NEW.status=3);
END$$
DROP TRIGGER IF EXISTS hub_game_task_count_update$$
CREATE TRIGGER hub_game_task_count_update AFTER UPDATE ON delivery_task FOR EACH ROW FOLLOWS hub_task_count_update
BEGIN
    IF OLD.game_id=NEW.game_id AND MOD(OLD.id,64)=MOD(NEW.id,64) THEN
        IF OLD.status<>NEW.status THEN
            UPDATE game_delivery_task_counts SET pending_count=pending_count+(NEW.status=0)-(OLD.status=0),
                delivering_count=delivering_count+(NEW.status=1)-(OLD.status=1),
                done_count=done_count+(NEW.status=2)-(OLD.status=2),
                failed_count=failed_count+(NEW.status=3)-(OLD.status=3)
            WHERE game_id=NEW.game_id AND bucket_id=MOD(NEW.id,64);
        END IF;
    ELSE
        UPDATE game_delivery_task_counts SET pending_count=pending_count-(OLD.status=0),
            delivering_count=delivering_count-(OLD.status=1),done_count=done_count-(OLD.status=2),
            failed_count=failed_count-(OLD.status=3) WHERE game_id=OLD.game_id AND bucket_id=MOD(OLD.id,64);
        INSERT INTO game_delivery_task_counts(game_id,bucket_id,pending_count,delivering_count,done_count,failed_count)
        VALUES(NEW.game_id,MOD(NEW.id,64),NEW.status=0,NEW.status=1,NEW.status=2,NEW.status=3)
        ON DUPLICATE KEY UPDATE pending_count=pending_count+(NEW.status=0),
            delivering_count=delivering_count+(NEW.status=1),done_count=done_count+(NEW.status=2),
            failed_count=failed_count+(NEW.status=3);
    END IF;
END$$
DROP TRIGGER IF EXISTS hub_game_task_count_delete$$
CREATE TRIGGER hub_game_task_count_delete AFTER DELETE ON delivery_task FOR EACH ROW FOLLOWS hub_task_count_delete
BEGIN
    UPDATE game_delivery_task_counts SET pending_count=pending_count-(OLD.status=0),
        delivering_count=delivering_count-(OLD.status=1),done_count=done_count-(OLD.status=2),
        failed_count=failed_count-(OLD.status=3) WHERE game_id=OLD.game_id AND bucket_id=MOD(OLD.id,64);
END$$
DROP TRIGGER IF EXISTS hub_game_archive_count_insert$$
CREATE TRIGGER hub_game_archive_count_insert AFTER INSERT ON delivery_task_archive FOR EACH ROW FOLLOWS hub_archive_count_insert
BEGIN
    INSERT INTO game_delivery_task_counts(game_id,bucket_id,archived_count)
    VALUES(NEW.game_id,MOD(NEW.id,64),1)
    ON DUPLICATE KEY UPDATE archived_count=archived_count+1;
END$$
DROP TRIGGER IF EXISTS hub_game_archive_count_delete$$
CREATE TRIGGER hub_game_archive_count_delete AFTER DELETE ON delivery_task_archive FOR EACH ROW FOLLOWS hub_archive_count_delete
BEGIN
    UPDATE game_delivery_task_counts SET archived_count=archived_count-1
    WHERE game_id=OLD.game_id AND bucket_id=MOD(OLD.id,64);
END$$
DELIMITER ;

-- 两套计数均与业务 DML 同事务更新，归档、回滚、连接取消不产生单独提交。
INSERT INTO schema_version(version, installed_at)
SELECT 3, UTC_TIMESTAMP(6) WHERE @gamehub_source_version IN (1,2);

-- 投递观测与运行控制：记录投递尝试、配置回执和游戏运行控制。
-- 不回填或推测历史尝试，已有任务及其业务身份保持不变。
SET @gamehub_source_version = (SELECT MAX(version) FROM schema_version);
SET @gamehub_migration_sql = IF(@gamehub_source_version>=3, 'DO 0', 'INVALID GAMEHUB SCHEMA VERSION');
PREPARE gamehub_migration_guard FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_guard;
DEALLOCATE PREPARE gamehub_migration_guard;

SET @gamehub_migration_sql = IF(@gamehub_source_version=3,
    'CREATE TABLE delivery_attempt (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id BIGINT UNSIGNED NOT NULL,
    event_id BIGINT UNSIGNED NOT NULL,
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    server_id INT UNSIGNED NOT NULL,
    generation BIGINT UNSIGNED NOT NULL,
    attempt_number INT UNSIGNED NOT NULL,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    outcome VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT ''in_flight'',
    error_category VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '''',
    UNIQUE KEY uk_attempt_generation (task_id, generation),
    KEY idx_attempt_task (game_id, task_id, id),
    CONSTRAINT fk_attempt_event FOREIGN KEY (event_id) REFERENCES hub_event(id),
    CONSTRAINT ck_attempt_number CHECK (attempt_number > 0),
    CONSTRAINT ck_attempt_server CHECK (server_id > 0)
) ENGINE=InnoDB', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

-- 尝试不引用活动任务外键，完成任务移入归档后仍保留完整尝试历史。
SET @gamehub_migration_sql = IF(@gamehub_source_version=3,
    'ALTER TABLE delivery_task
    ADD KEY idx_game_created (game_id, created_at, id),
    ADD KEY idx_game_server_created (game_id, server_id, created_at, id),
    ADD KEY idx_game_status_server_created (game_id, status, server_id, created_at, id)', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

SET @gamehub_migration_sql = IF(@gamehub_source_version=3,
    'CREATE TABLE config_server_state (
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    server_id INT UNSIGNED NOT NULL,
    version BIGINT UNSIGNED NOT NULL,
    content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    reported_at DATETIME(6) NOT NULL,
    PRIMARY KEY (game_id, server_id),
    CONSTRAINT fk_server_config FOREIGN KEY (game_id, version) REFERENCES config_version(game_id, version),
    CONSTRAINT ck_server_config_id CHECK (server_id > 0),
    CONSTRAINT ck_server_config_version CHECK (version > 0)
) ENGINE=InnoDB', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

SET @gamehub_migration_sql = IF(@gamehub_source_version=3,
    'CREATE TABLE game_delivery_control (
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    version BIGINT UNSIGNED NOT NULL,
    enabled BOOLEAN NOT NULL,
    max_concurrent_deliveries INT UNSIGNED NOT NULL,
    max_concurrent_per_server INT UNSIGNED NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT ck_control_version CHECK (version > 0),
    CONSTRAINT ck_control_enabled CHECK (enabled IN (0,1)),
    CONSTRAINT ck_control_limits CHECK (max_concurrent_deliveries <= 32 AND max_concurrent_per_server <= 32)
) ENGINE=InnoDB', 'DO 0');
PREPARE gamehub_migration_step FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_step;
DEALLOCATE PREPARE gamehub_migration_step;

INSERT INTO schema_version(version, installed_at)
SELECT 4, UTC_TIMESTAMP(6) WHERE @gamehub_source_version=3;

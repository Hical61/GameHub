-- 停止所有 Hub 写入后，从版本 1 升级。DDL 失败时先检查对象，不要直接重跑。
SET @gamehub_migration_sql = IF((SELECT MAX(version) FROM schema_version)=1,
    'DO 0', 'INVALID GAMEHUB SCHEMA VERSION');
PREPARE gamehub_migration_guard FROM @gamehub_migration_sql;
EXECUTE gamehub_migration_guard;
DEALLOCATE PREPARE gamehub_migration_guard;

ALTER TABLE delivery_task
    ADD KEY idx_status_created (status, created_at, id),
    ADD KEY idx_game_archive (game_id, status, updated_at, id);
ALTER TABLE hub_event
    ADD COLUMN payload_redacted_at DATETIME(6) NULL,
    ADD KEY idx_game_redaction (game_id, payload_redacted_at, created_at, id);

CREATE TABLE delivery_task_archive (
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
) ENGINE=InnoDB;

-- 固定 64 个计数分片，避免每次状态变更都抢同一行。读统计只需汇总这 64 行。
CREATE TABLE delivery_task_counts (
    bucket_id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    pending_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    delivering_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    done_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    failed_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    archived_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    CONSTRAINT ck_counts_bucket CHECK (bucket_id<64)
) ENGINE=InnoDB;
INSERT INTO delivery_task_counts(bucket_id)
SELECT a.n+8*b.n
FROM (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3
      UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7) a
CROSS JOIN (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3
      UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7) b;

UPDATE delivery_task_counts c JOIN (
    SELECT MOD(id,64) bucket_id, SUM(status=0) pending_count, SUM(status=1) delivering_count,
           SUM(status=2) done_count, SUM(status=3) failed_count
    FROM delivery_task GROUP BY MOD(id,64)
) t ON t.bucket_id=c.bucket_id
SET c.pending_count=t.pending_count, c.delivering_count=t.delivering_count,
    c.done_count=t.done_count, c.failed_count=t.failed_count;

DELIMITER $$
CREATE TRIGGER hub_task_count_insert AFTER INSERT ON delivery_task FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET pending_count=pending_count+(NEW.status=0),
        delivering_count=delivering_count+(NEW.status=1), done_count=done_count+(NEW.status=2),
        failed_count=failed_count+(NEW.status=3) WHERE bucket_id=MOD(NEW.id,64);
END$$
CREATE TRIGGER hub_task_count_update AFTER UPDATE ON delivery_task FOR EACH ROW
BEGIN
    IF OLD.status<>NEW.status THEN
        UPDATE delivery_task_counts SET pending_count=pending_count+(NEW.status=0)-(OLD.status=0),
            delivering_count=delivering_count+(NEW.status=1)-(OLD.status=1),
            done_count=done_count+(NEW.status=2)-(OLD.status=2),
            failed_count=failed_count+(NEW.status=3)-(OLD.status=3) WHERE bucket_id=MOD(NEW.id,64);
    END IF;
END$$
CREATE TRIGGER hub_task_count_delete AFTER DELETE ON delivery_task FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET pending_count=pending_count-(OLD.status=0),
        delivering_count=delivering_count-(OLD.status=1), done_count=done_count-(OLD.status=2),
        failed_count=failed_count-(OLD.status=3) WHERE bucket_id=MOD(OLD.id,64);
END$$
CREATE TRIGGER hub_archive_count_insert AFTER INSERT ON delivery_task_archive FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET archived_count=archived_count+1 WHERE bucket_id=MOD(NEW.id,64);
END$$
CREATE TRIGGER hub_archive_count_delete AFTER DELETE ON delivery_task_archive FOR EACH ROW
BEGIN
    UPDATE delivery_task_counts SET archived_count=archived_count-1 WHERE bucket_id=MOD(OLD.id,64);
END$$
DELIMITER ;

-- 计数和业务 DML 同事务提交，回滚时计数也回滚。运行账号无需额外的 TRIGGER 权限。
INSERT INTO schema_version(version, installed_at) VALUES (2, UTC_TIMESTAMP(6));

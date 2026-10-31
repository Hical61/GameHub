-- GameHub 0.1 建表脚本。用迁移账号在 Hub 独立数据库中执行，运行账号不需要 DDL 权限。
-- 标识按 ASCII 区分大小写；时间由数据库生成，统一用 UTC。
CREATE TABLE schema_version (
    version INT UNSIGNED NOT NULL PRIMARY KEY,
    installed_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE hub_event (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    task_type VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    namespace_key VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    biz_key VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uk_event (game_id, task_type, namespace_key, biz_key)
) ENGINE=InnoDB;

CREATE TABLE delivery_task (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_id BIGINT UNSIGNED NOT NULL,
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    server_id INT UNSIGNED NOT NULL,
    status TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '0=pending 1=delivering 2=done 3=failed',
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    generation BIGINT UNSIGNED NOT NULL DEFAULT 0,
    next_retry_at DATETIME(6) NOT NULL,
    lease_until DATETIME(6) NULL,
    last_error VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uk_target (event_id, server_id),
    KEY idx_pending (status, next_retry_at, id),
    KEY idx_lease (status, lease_until, id),
    KEY idx_game_tasks (game_id, id),
    CONSTRAINT fk_task_event FOREIGN KEY (event_id) REFERENCES hub_event(id),
    CONSTRAINT ck_task_status CHECK (status IN (0, 1, 2, 3)),
    CONSTRAINT ck_task_server CHECK (server_id > 0)
) ENGINE=InnoDB;

CREATE TABLE config_head (
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    version BIGINT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB;

CREATE TABLE config_version (
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    version BIGINT UNSIGNED NOT NULL,
    content JSON NOT NULL,
    content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (game_id, version)
) ENGINE=InnoDB;

CREATE TABLE audit_log (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    actor VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    action VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    target_id BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    KEY idx_game_audit (game_id, id)
) ENGINE=InnoDB;

-- 版本号最后写。MySQL 无法把整份 DDL 一起回滚，执行失败后要停服检查，不能直接重跑。
INSERT INTO schema_version(version, installed_at) VALUES (1, UTC_TIMESTAMP(6));

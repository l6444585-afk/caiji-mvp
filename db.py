"""SQLite 数据库：原始消息表 + 解析后发单表 + Phase 2 同步状态."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "messages.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    self_id TEXT,
    user_id TEXT,
    group_id TEXT,
    group_name TEXT,
    sender_nickname TEXT,
    sender_card TEXT,
    sender_role TEXT,
    message_type TEXT,
    sub_type TEXT,
    raw_message TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    parsed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    price REAL,
    product_name TEXT,
    taobao_token TEXT,
    coupon_code TEXT,
    quantity INTEGER,
    raw_message TEXT,
    sync_status TEXT NOT NULL DEFAULT 'PENDING',
    sync_attempts INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT,
    last_sync_error TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_feeds_price ON feeds(price);
CREATE INDEX IF NOT EXISTS idx_feeds_parsed_at ON feeds(parsed_at);
"""

# 注意: 依赖新列的索引 (idx_feeds_sync) 必须放 MIGRATIONS 而不是 SCHEMA,
# 否则在老 DB 上 executescript 会 fail (列还没加索引就建), 整个事务回滚, 连 ALTER 都不会执行.
# 教训 2026-05-14: 第一版把 idx_feeds_sync 写进 SCHEMA, 老 DB 升级时 executescript 抛
# `no such column: sync_status`, FastAPI 任何 DB 操作返 500.
MIGRATIONS = (
    "ALTER TABLE feeds ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'PENDING'",
    "ALTER TABLE feeds ADD COLUMN sync_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE feeds ADD COLUMN synced_at TEXT",
    "ALTER TABLE feeds ADD COLUMN last_sync_error TEXT",
    "CREATE INDEX IF NOT EXISTS idx_feeds_sync ON feeds(sync_status, sync_attempts)",
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """老 DB 升级: 跳过已存在的列/索引."""
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column name" in msg:
                continue
            if "already exists" in msg:
                continue
            raise
    conn.commit()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")

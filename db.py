"""SQLite 数据库：原始消息表 + 解析后发单表."""
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
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_feeds_price ON feeds(price);
CREATE INDEX IF NOT EXISTS idx_feeds_parsed_at ON feeds(parsed_at);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")

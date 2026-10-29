# -*- coding: utf-8 -*-
"""SQLite 持久化：消息历史、滚动摘要、去重。

只用标准库 sqlite3，Docker 镜像里不需要额外服务。
WAL 模式 + 单连接 + 线程锁，足够单机机器人用。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT    NOT NULL,
    msg_id      TEXT    NOT NULL DEFAULT '',
    sender_id   TEXT    NOT NULL DEFAULT '',
    sender_name TEXT    NOT NULL DEFAULT '',
    is_self     INTEGER NOT NULL DEFAULT 0,
    ts          INTEGER NOT NULL DEFAULT 0,
    kind        TEXT    NOT NULL DEFAULT 'text',
    content     TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_chat_msg
    ON messages(chat_id, msg_id) WHERE msg_id <> '';
CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS summaries (
    chat_id    TEXT PRIMARY KEY,
    summary    TEXT    NOT NULL DEFAULT '',
    last_rowid INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# 单条消息入库上限，防止有人贴长文把库撑爆
MAX_STORED_CHARS = 2000


@dataclass(slots=True)
class StoredMessage:
    rowid: int
    chat_id: str
    msg_id: str
    sender_id: str
    sender_name: str
    is_self: bool
    ts: int
    kind: str
    content: str

    @property
    def display_name(self) -> str:
        if self.is_self:
            return "我"
        return self.sender_name or "群友"


def _to_message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        rowid=row["id"],
        chat_id=row["chat_id"],
        msg_id=row["msg_id"],
        sender_id=row["sender_id"],
        sender_name=row["sender_name"],
        is_self=bool(row["is_self"]),
        ts=row["ts"],
        kind=row["kind"],
        content=row["content"],
    )


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ 写
    def add_message(
        self,
        chat_id: str,
        content: str,
        *,
        sender_id: str = "",
        sender_name: str = "",
        is_self: bool = False,
        ts: int | float = 0,
        kind: str = "text",
        msg_id: str = "",
    ) -> bool:
        """写入一条消息；重复的 msg_id 会被忽略并返回 False。"""
        content = (content or "").strip()
        if not content:
            return False
        content = content[:MAX_STORED_CHARS]
        when = int(ts or time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO messages"
                " (chat_id, msg_id, sender_id, sender_name, is_self, ts, kind, content)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, msg_id or "", sender_id, sender_name, 1 if is_self else 0, when, kind, content),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def seen(self, chat_id: str, msg_id: str) -> bool:
        """这条消息是否已经处理过（网关重连时会重复推送）。"""
        if not msg_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND msg_id = ? LIMIT 1",
                (chat_id, msg_id),
            ).fetchone()
        return row is not None
    def reset_chat(self, chat_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM summaries WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ 读
    def recent(self, chat_id: str, limit: int) -> list[StoredMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, max(1, int(limit))),
            ).fetchall()
        return [_to_message(row) for row in reversed(rows)]

    def after(self, chat_id: str, rowid: int, limit: int) -> list[StoredMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (chat_id, int(rowid), max(1, int(limit))),
            ).fetchall()
        return [_to_message(row) for row in rows]

    def count_new(self, chat_id: str, after_rowid: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ? AND id > ?",
                (chat_id, int(after_rowid)),
            ).fetchone()
        return int(row["n"]) if row else 0

    def last_rowid(self, chat_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(id) AS m FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return int(row["m"] or 0) if row else 0

    def get_summary(self, chat_id: str) -> tuple[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary, last_rowid FROM summaries WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if not row:
            return "", 0
        return row["summary"] or "", int(row["last_rowid"] or 0)

    def set_summary(self, chat_id: str, summary: str, last_rowid: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries (chat_id, summary, last_rowid, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(chat_id) DO UPDATE SET"
                " summary = excluded.summary,"
                " last_rowid = excluded.last_rowid,"
                " updated_at = excluded.updated_at",
                (chat_id, summary, int(last_rowid), int(time.time())),
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            chats = self._conn.execute("SELECT COUNT(DISTINCT chat_id) AS n FROM messages").fetchone()
            msgs = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return {"chats": int(chats["n"]), "messages": int(msgs["n"])}

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
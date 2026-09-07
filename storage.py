"""SQLite storage for managed-chat members observed by the bot."""

from __future__ import annotations
import sqlite3
from contextlib import contextmanager

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY (chat_id, user_id)
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.execute(_SCHEMA)


def upsert_member(
    chat_id: int, user_id: int, username: str | None, status: str
) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO members (chat_id, user_id, username, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET username = excluded.username, status = excluded.status
            """,
            (chat_id, user_id, username, status),
        )


def remove_member(chat_id: int, user_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM members WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )


def list_active_members(chat_id: int) -> list[tuple[int, str | None]]:
    """Return regular and restricted users who are still group members."""
    with _conn() as c:
        cur = c.execute(
            """
            SELECT user_id, username FROM members
            WHERE chat_id = ? AND status IN ('member', 'restricted')
            """,
            (chat_id,),
        )
        return cur.fetchall()


def find_member_by_username(
    chat_id: int, username: str
) -> tuple[int, str | None] | None:
    normalized = username.strip().lstrip("@").lower()
    if not normalized:
        return None
    with _conn() as c:
        return c.execute(
            """
            SELECT user_id, username FROM members
            WHERE chat_id = ? AND lower(username) = ?
            ORDER BY user_id LIMIT 1
            """,
            (chat_id, normalized),
        ).fetchone()


def list_tracked_chats() -> list[int]:
    with _conn() as c:
        cur = c.execute("SELECT DISTINCT chat_id FROM members")
        return [row[0] for row in cur.fetchall()]

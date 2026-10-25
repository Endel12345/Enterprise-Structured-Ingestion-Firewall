"""Small SQLite repository for conversations and messages.

Uses Python's built-in sqlite3 module, so no database server or extra package
is needed. The database file (SQLITE_PATH, default backend/data/chat.db) and its
tables are created automatically on startup.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import SQLITE_PATH

MAX_HISTORY_MESSAGES = 12  # Most recent six user/assistant exchanges sent to the LLM.

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Chat',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages(conversation_id, created_at);
"""

# Module-level so tests can point it at a temporary file.
db_path = Path(SQLITE_PATH)


def _now() -> str:
    # ISO-8601 UTC strings sort correctly as text.
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:  # commits on success, rolls back on error
            yield conn
    finally:
        conn.close()


def initialize_database() -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        # WAL lets the API read history while another request is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def ensure_conversation(session_id: str, title: str = "New Chat") -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, title[:100] or "New Chat", now, now),
        )


def add_chat_message(session_id: str, role: str, content: str) -> None:
    ensure_conversation(session_id, content[:70] if role == "user" else "New Chat")
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = 'New Chat' AND ? = 'user' THEN ? ELSE title END WHERE id = ?",
            (now, role, content[:70] or "New Chat", session_id),
        )


def get_chat_history(session_id: str) -> list[dict[str, str]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, MAX_HISTORY_MESSAGES),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def list_conversations(limit: int = 30) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.updated_at,
            (SELECT content FROM messages m WHERE m.conversation_id = c.id AND m.role = 'user' ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS preview
            FROM conversations c ORDER BY c.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"session_id": r[0], "title": r[1], "updated_at": r[2], "preview": r[3] or "Start a conversation"} for r in rows]


def get_conversation_messages(session_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]

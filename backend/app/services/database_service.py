"""Small SQLite repository for conversations and messages.

Uses Python's built-in sqlite3 module, so no database server or extra package
is needed. The database file (SQLITE_PATH, default backend/data/chat.db) and its
tables are created automatically on startup.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import ARCHIVE_RETENTION_DAYS, SQLITE_PATH

MAX_HISTORY_MESSAGES = 12  # Most recent six user/assistant exchanges sent to the LLM.

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Chat',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT  -- NULL = active; set = archived, permanently deleted after ARCHIVE_RETENTION_DAYS
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


def _purge_date(archived_at: str) -> str:
    return (datetime.fromisoformat(archived_at) + timedelta(days=ARCHIVE_RETENTION_DAYS)).isoformat()


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
        # Databases created before archiving existed don't have the column yet: add it.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
        if "archived_at" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_archived ON conversations(archived_at)")


def check_database() -> bool:
    """True if the database file can be opened and the tables exist (used by /health)."""
    try:
        with _connect() as conn:
            conn.execute("SELECT 1 FROM conversations LIMIT 1")
        return True
    except sqlite3.Error:
        return False


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
        # Writing in an archived chat brings it back (archived_at = NULL), like ChatGPT.
        conn.execute(
            "UPDATE conversations SET updated_at = ?, archived_at = NULL, title = CASE WHEN title = 'New Chat' AND ? = 'user' THEN ? ELSE title END WHERE id = ?",
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
            FROM conversations c WHERE c.archived_at IS NULL ORDER BY c.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"session_id": r[0], "title": r[1], "updated_at": r[2], "preview": r[3] or "Start a conversation"} for r in rows]


def archive_conversation(session_id: str) -> bool:
    """Move a chat to the archive. Returns False if it doesn't exist (or is already archived)."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE conversations SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (_now(), session_id),
        )
    return cursor.rowcount > 0


def restore_conversation(session_id: str) -> bool:
    """Bring an archived chat back. Returns False if it isn't in the archive."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE conversations SET archived_at = NULL WHERE id = ? AND archived_at IS NOT NULL",
            (session_id,),
        )
    return cursor.rowcount > 0


def list_archived_conversations() -> list[dict[str, Any]]:
    """Archived chats, most recently archived first, with the date each will be deleted."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, archived_at FROM conversations WHERE archived_at IS NOT NULL ORDER BY archived_at DESC"
        ).fetchall()
    return [
        {"session_id": r[0], "title": r[1], "archived_at": r[2], "delete_at": _purge_date(r[2])}
        for r in rows
    ]


def purge_expired_archives() -> int:
    """Permanently delete chats archived more than ARCHIVE_RETENTION_DAYS ago. Returns how many."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS)).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM conversations WHERE archived_at IS NOT NULL AND archived_at < ?",
            (cutoff,),
        )
    return cursor.rowcount


def get_archive_status(session_id: str) -> dict[str, str] | None:
    """{"archived_at", "delete_at"} if the chat is archived, otherwise None."""
    with _connect() as conn:
        row = conn.execute("SELECT archived_at FROM conversations WHERE id = ?", (session_id,)).fetchone()
    if not row or not row[0]:
        return None
    return {"archived_at": row[0], "delete_at": _purge_date(row[0])}


def rename_conversation(session_id: str, title: str) -> bool:
    """Change a conversation's title. Returns False if it doesn't exist."""
    with _connect() as conn:
        cursor = conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title[:100], session_id))
    return cursor.rowcount > 0


def delete_conversation(session_id: str) -> bool:
    """Delete a conversation and (via ON DELETE CASCADE) all its messages."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (session_id,))
    return cursor.rowcount > 0


def get_conversation_messages(session_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]

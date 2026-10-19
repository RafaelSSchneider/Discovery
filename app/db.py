"""Acesso ao Postgres (async, psycopg 3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import config

Row = dict[str, Any]

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_ref (
    owner_id   text PRIMARY KEY,
    ref        jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chats (
    id           text PRIMARY KEY,
    num          serial UNIQUE,
    title        text NOT NULL,
    chat_type    text,
    last_updated timestamptz,
    monitored    boolean NOT NULL DEFAULT false,
    last_synced  timestamptz
);
CREATE TABLE IF NOT EXISTS messages (
    chat_id     text NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    id          text NOT NULL,
    author      text NOT NULL,
    body        text NOT NULL,
    created_at  timestamptz NOT NULL,
    modified_at timestamptz,
    deleted     boolean NOT NULL DEFAULT false,
    PRIMARY KEY (chat_id, id)
);
CREATE INDEX IF NOT EXISTS messages_chat_created ON messages (chat_id, created_at);
CREATE TABLE IF NOT EXISTS reminders (
    id         serial PRIMARY KEY,
    text       text NOT NULL,
    due_at     timestamptz NOT NULL,
    sent       boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kv (
    key   text PRIMARY KEY,
    value text NOT NULL
);
ALTER TABLE chats ADD COLUMN IF NOT EXISTS learned_until timestamptz;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS author_id text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS checked boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS messages_unchecked ON messages (checked) WHERE NOT checked;

-- memória por pessoa
CREATE TABLE IF NOT EXISTS people (
    id         serial PRIMARY KEY,
    name       text NOT NULL,
    user_id    text UNIQUE,
    aliases    text[] NOT NULL DEFAULT '{}',
    priority   smallint NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS people_name_lower ON people (lower(name));
ALTER TABLE people ADD COLUMN IF NOT EXISTS priority_manual boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS learn_log (
    id         serial PRIMARY KEY,
    person_id  int NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    fact_id    int,
    kind       text NOT NULL,
    text       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS person_facts (
    id         serial PRIMARY KEY,
    person_id  int NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    fact       text NOT NULL,
    source     text NOT NULL DEFAULT 'manual',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS person_suggestions (
    id         serial PRIMARY KEY,
    person_id  int NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    fact       text,
    priority   smallint CHECK (priority BETWEEN 1 AND 3),
    reason     text,
    status     text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

_pool: AsyncConnectionPool | None = None


async def _get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            config.DATABASE_URL,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        await _pool.open(wait=True, timeout=60)
    return _pool


async def q(sql: str, params: Any = None) -> list[Row]:
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else []


async def q1(sql: str, params: Any = None) -> Row | None:
    rows = await q(sql, params)
    return rows[0] if rows else None


async def init() -> None:
    """Cria o schema (com lock, porque bot e worker sobem juntos)."""
    pool = await _get_pool()
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(424242)")
        try:
            await conn.execute(SCHEMA)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(424242)")


# ---------- conversation reference ----------

async def save_ref(owner_id: str, ref: dict[str, Any]) -> None:
    await q(
        """INSERT INTO conversation_ref (owner_id, ref) VALUES (%s, %s)
           ON CONFLICT (owner_id) DO UPDATE SET ref = EXCLUDED.ref, updated_at = now()""",
        (owner_id, Jsonb(ref)),
    )


async def load_ref(owner_id: str) -> dict[str, Any] | None:
    row = await q1("SELECT ref FROM conversation_ref WHERE owner_id = %s", (owner_id,))
    return row["ref"] if row else None


# ---------- chats ----------

async def upsert_chat(chat_id: str, title: str, chat_type: str | None, last_updated: datetime | None) -> None:
    # UPDATE-then-INSERT para não queimar a sequence de "num" a cada sync
    await q(
        """WITH up AS (
               UPDATE chats SET title = %(title)s, chat_type = %(type)s, last_updated = %(lu)s
               WHERE id = %(id)s RETURNING id
           )
           INSERT INTO chats (id, title, chat_type, last_updated)
           SELECT %(id)s, %(title)s, %(type)s, %(lu)s
           WHERE NOT EXISTS (SELECT 1 FROM up)""",
        {"id": chat_id, "title": title, "type": chat_type, "lu": last_updated},
    )


async def list_chats(search: str | None = None, limit: int = 30) -> list[Row]:
    return await q(
        """SELECT num, title, chat_type, monitored FROM chats
           WHERE %(s)s::text IS NULL OR title ILIKE '%%' || %(s)s || '%%'
           ORDER BY last_updated DESC NULLS LAST LIMIT %(lim)s""",
        {"s": search or None, "lim": limit},
    )


async def chat_by_num(num: int) -> Row | None:
    return await q1("SELECT * FROM chats WHERE num = %s", (num,))


async def set_monitored(num: int, monitored: bool) -> Row | None:
    return await q1(
        "UPDATE chats SET monitored = %s WHERE num = %s RETURNING num, title",
        (monitored, num),
    )


async def monitored_chats() -> list[Row]:
    return await q("SELECT * FROM chats WHERE monitored ORDER BY num")


async def set_last_synced(chat_id: str, ts: datetime) -> None:
    await q("UPDATE chats SET last_synced = %s WHERE id = %s", (ts, chat_id))


# ---------- mensagens ----------

async def upsert_messages(rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    pool = await _get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO messages (chat_id, id, author, author_id, body, created_at, modified_at, deleted)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (chat_id, id) DO UPDATE
               SET body = EXCLUDED.body, modified_at = EXCLUDED.modified_at,
                   deleted = EXCLUDED.deleted, author_id = EXCLUDED.author_id""",
            rows,
        )


async def messages_since(chat_id: str, since: datetime, limit: int = 3000) -> list[Row]:
    return await q(
        """SELECT author, author_id, body, created_at FROM messages
           WHERE chat_id = %s AND created_at >= %s AND NOT deleted AND body <> ''
           ORDER BY created_at LIMIT %s""",
        (chat_id, since, limit),
    )


# ---------- lembretes ----------

async def add_reminder(text: str, due_at: datetime) -> int:
    row = await q1("INSERT INTO reminders (text, due_at) VALUES (%s, %s) RETURNING id", (text, due_at))
    assert row is not None
    return int(row["id"])


async def due_reminders() -> list[Row]:
    return await q("SELECT id, text FROM reminders WHERE NOT sent AND due_at <= now() ORDER BY due_at")


async def pending_reminders() -> list[Row]:
    return await q("SELECT id, text, due_at FROM reminders WHERE NOT sent ORDER BY due_at")


async def mark_sent(reminder_id: int) -> None:
    await q("UPDATE reminders SET sent = true WHERE id = %s", (reminder_id,))


async def cancel_reminder(reminder_id: int) -> bool:
    row = await q1("DELETE FROM reminders WHERE id = %s AND NOT sent RETURNING id", (reminder_id,))
    return row is not None


# ---------- kv ----------

async def kv_get(key: str) -> str | None:
    row = await q1("SELECT value FROM kv WHERE key = %s", (key,))
    return row["value"] if row else None


async def kv_set(key: str, value: str) -> None:
    await q(
        "INSERT INTO kv (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


async def kv_pop(key: str) -> str | None:
    row = await q1("DELETE FROM kv WHERE key = %s RETURNING value", (key,))
    return row["value"] if row else None

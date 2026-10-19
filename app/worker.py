"""Sincronização: sincroniza lista de chats e mensagens dos chats acompanhados."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, learn, people
from .graph import Graph, NotLoggedInError
from .textutil import graph_iso, html_to_text, parse_dt

log = logging.getLogger("worker")

CHAT_LIST_INTERVAL = 1800  # segundos entre sincronizações da lista de chats


def _author(msg: dict[str, Any]) -> tuple[str, str | None]:
    frm = msg.get("from") or {}
    who = frm.get("user") or frm.get("application") or {}
    return str(who.get("displayName") or "?"), who.get("id")


async def sync_chats(graph: Graph, me_id: str) -> int:
    count = 0
    async for chat in graph.paged("/me/chats", {"$expand": "members", "$top": "50"}):
        others = [
            m.get("displayName") or "?"
            for m in chat.get("members", [])
            if m.get("userId") != me_id
        ]
        title = chat.get("topic") or ", ".join(others) or "(sem nome)"
        await db.upsert_chat(
            chat["id"], title[:300], chat.get("chatType"), parse_dt(chat.get("lastUpdatedDateTime"))
        )
        count += 1
    return count


async def sync_messages(graph: Graph, chat: dict[str, Any]) -> int:
    since: datetime = chat["last_synced"] or (
        datetime.now(timezone.utc) - timedelta(days=config.BACKFILL_DAYS)
    )
    params = {
        "$top": "50",
        "$orderby": "lastModifiedDateTime desc",
        "$filter": f"lastModifiedDateTime gt {graph_iso(since)}",
    }
    newest = since
    rows: list[tuple[Any, ...]] = []
    async for msg in graph.paged(f"/chats/{chat['id']}/messages", params):
        if msg.get("messageType") != "message":
            continue
        body = html_to_text((msg.get("body") or {}).get("content", ""))
        if not body and msg.get("attachments"):
            body = "[anexo]"
        created = parse_dt(msg.get("createdDateTime"))
        modified = parse_dt(msg.get("lastModifiedDateTime")) or created
        if created is None:
            continue
        author, author_id = _author(msg)
        rows.append(
            (chat["id"], msg["id"], author, author_id, body, created, modified, bool(msg.get("deletedDateTime")))
        )
        if modified and modified > newest:
            newest = modified
    await db.upsert_messages(rows)
    await db.set_last_synced(chat["id"], newest)
    return len(rows)


async def wait_next_cycle() -> bool:
    """Dorme até o próximo ciclo; retorna True se um comando pediu sync imediato."""
    for _ in range(max(1, config.POLL_INTERVAL // 15)):
        if await db.kv_pop("sync_requested"):
            return True
        await asyncio.sleep(15)
    return False


async def sync_loop(graph: Graph) -> None:
    me_id: str | None = None
    last_chat_sync = 0.0
    force = True

    while True:
        try:
            if me_id is None:
                me_id = str((await graph.get("/me"))["id"])
                await db.kv_set("me_id", me_id)
            if force or time.monotonic() - last_chat_sync > CHAT_LIST_INTERVAL:
                n = await sync_chats(graph, me_id)
                last_chat_sync = time.monotonic()
                log.info("Lista de chats sincronizada: %s", n)
            budget = config.LEARN_MAX_CALLS
            for chat in await db.monitored_chats():
                n = await sync_messages(graph, chat)
                if n:
                    log.info("%s: %s mensagens novas/alteradas", chat["title"], n)
                try:
                    budget -= await learn.run(chat, budget)
                except Exception:
                    log.exception("Erro no aprendizado (%s)", chat["title"])
            await people.link_people()
            await db.kv_set("worker_status", "ok")
        except NotLoggedInError as exc:
            log.warning("Sem login (%s). Rode no console do worker: python -m app.login", exc)
            await db.kv_set("worker_status", "login_required")
        except Exception:
            log.exception("Erro no ciclo do worker")
        force = await wait_next_cycle()

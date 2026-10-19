"""Conversa pelo chat de notas pessoais do Teams (48:notes), só com a Graph."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config, db
from .graph import Graph, NotLoggedInError
from .textutil import html_to_text, parse_dt

log = logging.getLogger("selfchat")

PREFIX = "🤖"  # marca as respostas do bot; poll_commands as ignora
MAX_CHARS = 8000
SEEN_KEEP = 200
NOT_FOUND_BACKOFF = 300

_graph: Graph | None = None

_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")


def bind(graph: Graph) -> None:
    global _graph
    _graph = graph


def _inline(text: str) -> str:
    parts = _CODE.split(html.escape(text, quote=False))
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{part}</code>")
        else:
            part = _BOLD.sub(r"<strong>\1</strong>", part)
            out.append(_ITALIC.sub(r"<em>\1</em>", part))
    return "".join(out)


def md_to_html(text: str) -> str:
    """Markdown simples das respostas (negrito, itálico, código, títulos, listas) em HTML escapado."""
    lines: list[str] = []
    for raw in text.split("\n"):
        if m := _HEADING.match(raw):
            lines.append(f"<strong>{_inline(m.group(1))}</strong>")
        elif m := _BULLET.match(raw):
            lines.append("• " + _inline(m.group(1)))
        elif raw.strip() == "---":
            lines.append("<hr>")
        elif raw.startswith(">"):
            lines.append("│ " + _inline(raw[1:].lstrip()))
        else:
            lines.append(_inline(raw))
    return "<br>".join(lines)


def split_message(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Quebra em parágrafos (depois linhas, depois caracteres) sem passar de `limit`."""
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        pieces = [para]
        if len(para) > limit:
            pieces = [ln[i : i + limit] for ln in para.split("\n") for i in range(0, max(len(ln), 1), limit)]
        sep = "\n\n" if len(pieces) == 1 else "\n"
        for piece in pieces:
            if cur and len(cur) + len(sep) + len(piece) > limit:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur}{sep}{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


async def _post(graph: Graph, text: str) -> None:
    for chunk in split_message(text):
        body = {"body": {"contentType": "html", "content": f"{PREFIX} {md_to_html(chunk)}"}}
        await graph.post(f"/chats/{config.NOTES_CHAT_ID}/messages", body)


async def send_to_owner(text: str) -> None:
    """Posta no chat de notas, sempre com o prefixo 🤖, em várias mensagens se for longo."""
    assert _graph is not None, "selfchat.bind(graph) não foi chamado"
    await _post(_graph, text)


async def notify_email(subject: str, text: str) -> None:
    """Cópia por e-mail para o próprio usuário (NOTIFY_EMAIL): o chat de notas pode não notificar o celular.
    Nunca levanta: uma falha de e-mail não pode repetir o envio no chat."""
    if not config.NOTIFY_EMAIL:
        return
    try:
        await _send_email(subject, text)
    except Exception:
        log.exception("Falha ao enviar e-mail")


async def _send_email(subject: str, text: str) -> None:
    assert _graph is not None, "selfchat.bind(graph) não foi chamado"
    address = await db.kv_get("me_mail")
    if not address:
        me = await _graph.get("/me")
        address = str(me.get("mail") or me.get("userPrincipalName") or "")
        if not address:
            log.warning("Sem endereço de e-mail do usuário; e-mail não enviado.")
            return
        await db.kv_set("me_mail", address)
    message = {
        "subject": f"{PREFIX} {subject}",
        "body": {"contentType": "html", "content": md_to_html(text)},
        "toRecipients": [{"emailAddress": {"address": address}}],
    }
    await _graph.post("/me/sendMail", {"message": message, "saveToSentItems": False})


# ---------- leitura de comandos ----------

def _sender_id(msg: dict[str, Any]) -> str | None:
    return ((msg.get("from") or {}).get("user") or {}).get("id")


async def poll_once(
    graph: Graph,
    handle: Callable[[str], Awaitable[str]],
    handle_free_text: Callable[[str], Awaitable[str]],
) -> int:
    """Executa os comandos novos do chat de notas; devolve quantos executou."""
    watermark = await db.kv_get("cmd_watermark")
    if not watermark:
        await db.kv_set("cmd_watermark", datetime.now(timezone.utc).isoformat())  # ignora notas antigas
        return 0
    me_id = await db.kv_get("me_id")
    if not me_id:  # o sync pode demorar até um ciclo inteiro para gravar isso após o login
        me_id = str((await graph.get("/me"))["id"])
        await db.kv_set("me_id", me_id)

    since = parse_dt(watermark)
    assert since is not None
    data = await graph.get(
        f"/chats/{config.NOTES_CHAT_ID}/messages", {"$top": "50", "$orderby": "createdDateTime desc"}
    )
    seen: list[str] = json.loads(await db.kv_get("cmd_seen") or "[]")
    fresh = []
    for msg in data.get("value", []):
        created = parse_dt(msg.get("createdDateTime"))
        if created and created >= since and msg["id"] not in seen:
            fresh.append((created, msg))
    fresh.sort(key=lambda item: item[0])

    ran = 0
    for created, msg in fresh:
        seen.append(msg["id"])
        # marca antes de executar: um comando que falha nunca é reexecutado em loop
        await db.kv_set("cmd_seen", json.dumps(seen[-SEEN_KEEP:]))
        await db.kv_set("cmd_watermark", created.isoformat())
        if msg.get("messageType") != "message" or _sender_id(msg) != me_id:
            continue
        text = html_to_text((msg.get("body") or {}).get("content", ""))
        if text.startswith(PREFIX):
            continue
        if text.startswith("/"):
            run = handle
        elif text.startswith("!") and text[1:].strip():
            run, text = handle_free_text, text[1:].strip()
        else:
            continue
        ran += 1
        try:
            reply = await run(text)
        except Exception as exc:
            log.exception("Falha no comando %r", text[:60])
            reply = f"⚠️ Erro interno: {exc}"
        await _post(graph, reply)
    return ran


async def poll_commands(
    graph: Graph,
    handle: Callable[[str], Awaitable[str]],
    handle_free_text: Callable[[str], Awaitable[str]],
) -> None:
    while True:
        wait = config.COMMAND_POLL_SECONDS
        try:
            await poll_once(graph, handle, handle_free_text)
            await db.kv_set("selfchat_status", "ok")
        except NotLoggedInError as exc:
            log.warning("Sem login (%s); comandos pausados até o `python -m app.login`.", exc)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                msg = f"chat de notas ({config.NOTES_CHAT_ID}) não encontrado (404): abra o chat de notas no Teams e envie uma mensagem"
                log.error(msg)
                await db.kv_set("selfchat_status", msg)
                wait = NOT_FOUND_BACKOFF
            else:
                log.exception("Erro ao ler o chat de notas")
        except Exception:
            log.exception("Erro ao ler o chat de notas")
        await asyncio.sleep(wait)

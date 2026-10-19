"""Aprendizado automático: conforme lê os chats, o assistente cria memórias sobre as pessoas."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, llm, people
from .people import PRIORITY_LABEL

log = logging.getLogger(__name__)
Row = dict[str, Any]
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
MODES = {"auto": "auto", "revisar": "review", "review": "review", "off": "off"}


async def mode() -> str:
    return MODES.get((await db.kv_get("learn_mode")) or config.LEARN_MODE, "auto")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


def _line(m: Row) -> str:
    return f"[{m['created_at'].astimezone(config.TZ):%d/%m %H:%M}] {m['author']}: {m['body']}"


def chunk(msgs: list[Row], max_chars: int) -> list[list[Row]]:
    chunks: list[list[Row]] = []
    cur: list[Row] = []
    size = 0
    for m in msgs:
        n = len(_line(m)) + 1
        if cur and size + n > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def match_author(name: str, authors: dict[str, tuple[str, str | None]]) -> tuple[str, str | None] | None:
    """Casa o nome citado pelo LLM com quem realmente escreveu (evita pessoas inventadas)."""
    key = name.lower().strip()
    if key in authors:
        return authors[key]
    words = set(key.split())
    cands = [v for k, v in authors.items() if words and (words <= set(k.split()) or set(k.split()) <= words)]
    return cands[0] if len(cands) == 1 else None


async def _add_fact(person: Row, fact: str) -> bool:
    fact = fact.strip()[:200]
    existing = await db.q("SELECT fact, source FROM person_facts WHERE person_id = %s", (person["id"],))
    if _norm(fact) in {_norm(r["fact"]) for r in existing}:
        return False
    if sum(1 for r in existing if r["source"] == "auto") >= config.AUTO_FACT_CAP:
        return False
    fid = await people.add_fact(person["id"], fact, "auto")
    await db.q(
        "INSERT INTO learn_log (person_id, fact_id, kind, text) VALUES (%s, %s, 'fact', %s)",
        (person["id"], fid, fact),
    )
    return True


async def _apply_priority(person: Row, prio: int, why: str | None) -> bool:
    if person["priority_manual"] or prio == person["priority"]:
        return False
    if person["priority"] == 2:  # ainda no padrão: classifica sozinho
        await people.set_priority(person["id"], prio, manual=False)
        text = f"{PRIORITY_LABEL[2]} → {PRIORITY_LABEL[prio]}" + (f" ({why})" if why else "")
        await db.q(
            "INSERT INTO learn_log (person_id, kind, text) VALUES (%s, 'priority', %s)", (person["id"], text)
        )
        return True
    dup = await db.q1(
        "SELECT 1 FROM person_suggestions WHERE person_id = %s AND priority = %s AND status = 'pending'",
        (person["id"], prio),
    )
    if not dup:  # já tinha sido classificada antes: mudar depois passa pela sua aprovação
        await db.q(
            "INSERT INTO person_suggestions (person_id, priority, reason) VALUES (%s, %s, %s)",
            (person["id"], prio, why),
        )
    return False


async def apply_auto(items: list[dict[str, Any]], authors: dict[str, tuple[str, str | None]]) -> int:
    me = await db.kv_get("me_id")
    owner = config.OWNER_NAME.lower()
    learned = 0
    for it in items:
        hit = match_author(str(it.get("person") or ""), authors)
        if not hit:
            continue
        display, uid = hit
        if (uid and uid == me) or (owner and display.lower() == owner):
            continue
        person = await people.get_or_create(display, uid)
        if it.get("fact"):
            learned += await _add_fact(person, str(it["fact"]))
        if it.get("priority") in (1, 2, 3):
            learned += await _apply_priority(person, int(it["priority"]), it.get("why"))
    return learned


async def _only_real(items: list[dict[str, Any]], authors: dict[str, tuple[str, str | None]]) -> list[dict[str, Any]]:
    """Modo revisar: troca o nome pelo de quem escreveu de fato e descarta nomes inventados e fatos repetidos."""
    out: list[dict[str, Any]] = []
    for it in items:
        hit = match_author(str(it.get("person") or ""), authors)
        if not hit:
            continue
        known = await people.find(hit[0])
        if it.get("fact") and known:
            facts = await db.q("SELECT fact FROM person_facts WHERE person_id = %s", (known[0]["id"],))
            if _norm(str(it["fact"])) in {_norm(f["fact"]) for f in facts}:
                it = {**it, "fact": None}
        out.append({**it, "person": hit[0]})
    return out


async def run(chat: Row, budget: int) -> int:
    """Aprende com as mensagens novas de um chat. Retorna quantas chamadas ao LLM usou."""
    m = await mode()
    if m == "off" or budget <= 0:
        return 0
    since = (chat.get("learned_until") or EPOCH) + timedelta(microseconds=1)
    msgs = await db.messages_since(chat["id"], since)
    if not msgs:
        return 0
    old_enough = datetime.now(timezone.utc) - msgs[0]["created_at"] > timedelta(hours=24)
    if len(msgs) < config.LEARN_MIN_MESSAGES and not old_enough:
        return 0  # espera juntar mais mensagens

    calls = 0
    for part in chunk(msgs, config.LEARN_MAX_CHARS):
        if calls >= budget:
            break
        ids = sorted({x["author_id"] for x in part if x.get("author_id")})
        names = sorted({x["author"] for x in part})
        ctx = await people.context_for(ids, names)
        transcript = "\n".join(_line(x) for x in part)
        try:
            items = await llm.suggest_memories(chat["title"], transcript, ctx)
        except Exception:
            log.exception("LLM falhou no aprendizado; tento de novo no próximo ciclo")
            break
        calls += 1
        authors = {x["author"].lower(): (x["author"], x.get("author_id")) for x in part}
        if m == "auto":
            n = await apply_auto(items, authors)
        else:
            n = len(await people.create_suggestions(await _only_real(items, authors), {k: v[1] for k, v in authors.items()}))
        if n:
            log.info("%s: %s memória(s) %s", chat["title"], n, "aprendida(s)" if m == "auto" else "sugerida(s)")
        await db.q("UPDATE chats SET learned_until = %s WHERE id = %s", (part[-1]["created_at"], chat["id"]))
    return calls


async def digest(since: datetime) -> str:
    rows = await db.q(
        """SELECT l.kind, l.text, l.fact_id, p.name
           FROM learn_log l JOIN people p ON p.id = l.person_id
           LEFT JOIN person_facts f ON f.id = l.fact_id
           WHERE l.created_at > %s AND (l.kind <> 'fact' OR f.id IS NOT NULL)
           ORDER BY l.created_at""",
        (since,),
    )
    pending = await people.pending_suggestions()
    if not rows and not pending:
        return ""
    lines = ["🧠 **O que aprendi**"]
    for r in rows:
        if r["kind"] == "fact":
            lines.append(f"🤖 **{r['name']}** — {r['text']} · `/apagar-nota {r['fact_id']}`")
        else:
            lines.append(f"🎚️ **{r['name']}** — prioridade {r['text']} · mude com `/prioridade`")
    if pending:
        lines.append(f"\n{len(pending)} sugestão(ões) aguardando você em `/sugestoes`.")
    return "\n\n".join(lines)

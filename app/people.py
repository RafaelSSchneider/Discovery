"""Memória por pessoa: prioridade de resposta, fatos, sugestões e alertas."""

from __future__ import annotations

from typing import Any

from . import config, db

Row = dict[str, Any]
PRIORITY_LABEL = {1: "🔴 alta", 2: "🟡 normal", 3: "⚪ baixa"}


# ---------- busca / criação ----------

async def find(query: str) -> list[Row]:
    exact = await db.q(
        """SELECT * FROM people
           WHERE lower(name) = lower(%(q)s)
              OR lower(%(q)s) IN (SELECT lower(a) FROM unnest(aliases) a)""",
        {"q": query},
    )
    if exact:
        return exact
    return await db.q(
        """SELECT * FROM people
           WHERE name ILIKE %(p)s OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE a ILIKE %(p)s)
           ORDER BY name LIMIT 10""",
        {"p": f"%{query}%"},
    )


async def get_or_create(name: str, user_id: str | None) -> Row:
    if user_id:
        row = await db.q1("SELECT * FROM people WHERE user_id = %s", (user_id,))
        if row:
            return row
    row = await db.q1("SELECT * FROM people WHERE lower(name) = lower(%s)", (name,))
    if row:
        if user_id and not row["user_id"]:
            row = await db.q1("UPDATE people SET user_id = %s WHERE id = %s RETURNING *", (user_id, row["id"]))
        assert row is not None
        return row
    new = await db.q1("INSERT INTO people (name, user_id) VALUES (%s, %s) RETURNING *", (name, user_id))
    assert new is not None
    return new


async def resolve(query: str, create: bool = False) -> tuple[Row | None, str | None]:
    """Acha a pessoa por nome/apelido; senão procura entre os autores das mensagens."""
    query = query.strip()
    if not query:
        return None, "Informe o nome da pessoa."
    matches = await find(query)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "Mais de uma pessoa: " + ", ".join(m["name"] for m in matches) + ". Seja mais específico."

    me = await db.kv_get("me_id")
    authors = await db.q(
        """SELECT DISTINCT ON (author_id) author, author_id FROM messages
           WHERE author ILIKE %s AND author_id IS NOT NULL AND author_id IS DISTINCT FROM %s
           LIMIT 10""",
        (f"%{query}%", me),
    )
    if len(authors) == 1:
        return await get_or_create(authors[0]["author"], authors[0]["author_id"]), None
    if len(authors) > 1:
        return None, "Encontrei: " + ", ".join(a["author"] for a in authors) + ". Seja mais específico."
    if not create:
        return None, f"Não conheço ninguém chamado *{query}*."
    return await get_or_create(query, None), None


async def link_people() -> None:
    """Liga pessoas criadas só pelo nome ao ID do Teams quando o nome aparece nas mensagens."""
    await db.q(
        """UPDATE people p SET user_id = sub.author_id
           FROM (SELECT DISTINCT ON (lower(author)) lower(author) AS a, author_id
                 FROM messages WHERE author_id IS NOT NULL) sub
           WHERE p.user_id IS NULL AND lower(p.name) = sub.a
             AND NOT EXISTS (SELECT 1 FROM people x WHERE x.user_id = sub.author_id)"""
    )


# ---------- edição ----------

async def add_fact(person_id: int, fact: str, source: str = "manual") -> int:
    row = await db.q1(
        "INSERT INTO person_facts (person_id, fact, source) VALUES (%s, %s, %s) RETURNING id",
        (person_id, fact.strip(), source),
    )
    await db.q("UPDATE people SET updated_at = now() WHERE id = %s", (person_id,))
    assert row is not None
    return int(row["id"])


async def delete_fact(fact_id: int) -> bool:
    return await db.q1("DELETE FROM person_facts WHERE id = %s RETURNING id", (fact_id,)) is not None


async def set_priority(person_id: int, priority: int, manual: bool = True) -> None:
    """manual=True trava a prioridade: o aprendizado automático não mexe mais nela."""
    await db.q(
        "UPDATE people SET priority = %s, priority_manual = priority_manual OR %s, updated_at = now() WHERE id = %s",
        (priority, manual, person_id),
    )


async def add_alias(person_id: int, alias: str) -> None:
    await db.q(
        """UPDATE people SET aliases = array_append(aliases, %(a)s)
           WHERE id = %(id)s AND NOT (%(a)s = ANY(aliases))""",
        {"a": alias.strip(), "id": person_id},
    )


# ---------- leitura ----------

async def list_people() -> list[Row]:
    return await db.q(
        """SELECT p.id, p.name, p.priority, count(f.id) AS facts
           FROM people p LEFT JOIN person_facts f ON f.person_id = p.id
           GROUP BY p.id ORDER BY p.priority, p.name"""
    )


async def card(person: Row) -> str:
    facts = await db.q(
        "SELECT id, fact, source FROM person_facts WHERE person_id = %s ORDER BY created_at", (person["id"],)
    )
    lines = [f"**{person['name']}** · prioridade {PRIORITY_LABEL[person['priority']]}"]
    if person["aliases"]:
        lines.append("Apelidos: " + ", ".join(person["aliases"]))
    if not person["user_id"]:
        lines.append("_(ainda não vinculado a um usuário do Teams)_")
    mark = {"sugerido": " _(sugerido)_", "auto": " 🤖"}
    lines += [f"`{f['id']}` {f['fact']}{mark.get(f['source'], '')}" for f in facts]
    if not facts:
        lines.append("Sem notas ainda.")
    return "\n\n".join(lines)


async def recent_messages(person: Row, since: Any, limit: int = 300) -> list[Row]:
    """Mensagens escritas pela pessoa nos chats acompanhados, da mais antiga para a mais nova."""
    rows = await db.q(
        """SELECT m.created_at, m.body, c.title
           FROM messages m JOIN chats c ON c.id = m.chat_id
           WHERE (m.author_id = %(uid)s OR lower(m.author) = lower(%(name)s))
             AND m.created_at > %(since)s AND m.body <> '' AND NOT m.deleted
           ORDER BY m.created_at DESC LIMIT %(limit)s""",
        {"uid": person["user_id"], "name": person["name"], "since": since, "limit": limit},
    )
    return rows[::-1]


async def context_for(author_ids: list[str], names: list[str]) -> str:
    """Texto com o que se sabe das pessoas citadas, para entrar no prompt do LLM."""
    rows = await db.q(
        """SELECT p.name, p.priority,
                  coalesce(array_agg(f.fact ORDER BY f.created_at) FILTER (WHERE f.id IS NOT NULL), '{}') AS facts
           FROM people p LEFT JOIN person_facts f ON f.person_id = p.id
           WHERE p.user_id = ANY(%(ids)s) OR lower(p.name) = ANY(%(names)s)
           GROUP BY p.id ORDER BY p.priority, p.name""",
        {"ids": author_ids, "names": [n.lower() for n in names]},
    )
    out = []
    for r in rows:
        facts = "; ".join(r["facts"][:15]) or "sem notas"
        out.append(f"- {r['name']} (prioridade {PRIORITY_LABEL[r['priority']]}): {facts}")
    return "\n".join(out)


# ---------- sugestões ----------

async def create_suggestions(items: list[dict[str, Any]], authors: dict[str, str | None]) -> list[Row]:
    """authors: nome em minúsculas -> user_id, de quem apareceu nas mensagens analisadas."""
    me = await db.kv_get("me_id")
    owner = config.OWNER_NAME.lower()
    created: list[Row] = []
    for it in items:
        name = str(it.get("person") or "").strip()
        if not name:
            continue
        uid = authors.get(name.lower())
        if (uid and uid == me) or (owner and name.lower() == owner):
            continue
        fact = (str(it["fact"]).strip() or None) if it.get("fact") else None
        prio = it.get("priority") if it.get("priority") in (1, 2, 3) else None
        if not fact and prio is None:
            continue
        person = await get_or_create(name, uid)
        if prio == person["priority"] and not fact:
            continue
        row = await db.q1(
            """INSERT INTO person_suggestions (person_id, fact, priority, reason)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (person["id"], fact, prio, it.get("why")),
        )
        assert row is not None
        created.append({"id": row["id"], "name": person["name"], "fact": fact, "priority": prio, "reason": it.get("why")})
    return created


def format_suggestion(s: Row) -> str:
    parts = []
    if s["fact"]:
        parts.append(s["fact"])
    if s["priority"]:
        why = f" ({s['reason']})" if s.get("reason") else ""
        parts.append(f"prioridade {PRIORITY_LABEL[s['priority']]}{why}")
    return f"`#{s['id']}` **{s['name']}** — " + " · ".join(parts)


async def pending_suggestions() -> list[Row]:
    return await db.q(
        """SELECT s.id, p.name, s.fact, s.priority, s.reason FROM person_suggestions s
           JOIN people p ON p.id = s.person_id WHERE s.status = 'pending' ORDER BY s.id"""
    )


async def review(ids: list[int] | None, approve: bool) -> int:
    """ids=None aplica a todas as pendentes."""
    rows = await db.q(
        """UPDATE person_suggestions SET status = %(st)s
           WHERE status = 'pending' AND (%(ids)s::int[] IS NULL OR id = ANY(%(ids)s::int[]))
           RETURNING person_id, fact, priority""",
        {"st": "approved" if approve else "rejected", "ids": ids},
    )
    if approve:
        for r in rows:
            if r["fact"]:
                await add_fact(r["person_id"], r["fact"], "sugerido")
            if r["priority"]:
                await set_priority(r["person_id"], r["priority"])
    return len(rows)


# ---------- prioridades de resposta ----------

async def pending_replies() -> list[Row]:
    """Por chat acompanhado: mensagens dos outros depois da sua última mensagem (até 7 dias)."""
    me = await db.kv_get("me_id")
    if not me:
        return []
    return await db.q(
        """WITH last_mine AS (
               SELECT chat_id, max(created_at) AS t FROM messages WHERE author_id = %(me)s GROUP BY chat_id
           ), waiting AS (
               SELECT m.chat_id, m.author, m.body, m.created_at, coalesce(p.priority, 2) AS priority
               FROM messages m
               JOIN chats c ON c.id = m.chat_id AND c.monitored
               LEFT JOIN last_mine lm ON lm.chat_id = m.chat_id
               LEFT JOIN people p ON p.user_id = m.author_id
               WHERE m.author_id IS DISTINCT FROM %(me)s AND NOT m.deleted AND m.body <> ''
                 AND m.created_at > coalesce(lm.t, now() - interval '2 days')
                 AND m.created_at > now() - interval '7 days'
           )
           SELECT c.num, c.title, c.chat_type,
                  min(w.priority) AS priority, count(*) AS n, min(w.created_at) AS since,
                  array_agg(DISTINCT w.author) AS authors,
                  (array_agg(w.author || ': ' || left(w.body, 140) ORDER BY w.created_at DESC))[1] AS last
           FROM waiting w JOIN chats c ON c.id = w.chat_id
           GROUP BY c.num, c.title, c.chat_type
           ORDER BY min(w.priority), min(w.created_at)
           LIMIT 15""",
        {"me": me},
    )


async def priority_alerts() -> list[Row]:
    """Marca mensagens novas como verificadas e devolve as de pessoas prioritárias."""
    me = await db.kv_get("me_id")
    return await db.q(
        """WITH cand AS (
               UPDATE messages SET checked = true WHERE NOT checked
               RETURNING chat_id, author, author_id, body, created_at
           )
           SELECT c.num, c.title, p.name, p.priority, cand.body, cand.created_at
           FROM cand
           JOIN chats c ON c.id = cand.chat_id
           JOIN people p ON p.user_id = cand.author_id
           WHERE p.priority <= %(maxp)s AND cand.author_id IS DISTINCT FROM %(me)s
             AND cand.body <> '' AND cand.created_at > now() - interval '2 hours'
           ORDER BY cand.created_at""",
        {"maxp": config.ALERT_PRIORITY, "me": me},
    )

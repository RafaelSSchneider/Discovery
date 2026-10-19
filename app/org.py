"""Organograma pelo diretório do Microsoft 365 (gestor, equipe direta, colegas)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config, db, learn, people
from .graph import Graph

log = logging.getLogger(__name__)
Row = dict[str, Any]

FIELDS = "id,displayName,jobTitle,department,mail"
MAX_LEVELS = 5
MAX_LIST = 15
NEEDS_SETUP = (
    "Organograma desligado ou sem permissão. Ative `ORG_CHART=true` na stack, adicione a permissão "
    "delegada `User.Read.All` no app (um admin pode precisar consentir) e refaça `python -m app.login`."
)


def _label(u: Row) -> str:
    extra = " · ".join(x for x in (u.get("jobTitle"), u.get("department")) if x)
    return f"**{u.get('displayName') or '?'}**" + (f" · {extra}" if extra else "")


async def _optional(graph: Graph, url: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
    try:
        return await graph.get(url, params)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:  # sem gestor / sem relação
            return None
        raise


async def find_user(graph: Graph, query: str) -> tuple[Row | None, str | None]:
    query = query.strip()
    if not query:
        me = await db.kv_get("me_id") or str((await graph.get("/me"))["id"])
        return await graph.get(f"/users/{me}", {"$select": FIELDS}), None
    if "@" in query:
        return await graph.get(f"/users/{query}", {"$select": FIELDS}), None
    person, _ = await people.resolve(query)  # aceita apelido e parte do nome
    name = (person["name"] if person else query).replace('"', " ")
    data = await graph.get(
        "/users",
        {"$search": f'"displayName:{name}"', "$select": FIELDS, "$top": "10"},
        {"ConsistencyLevel": "eventual"},
    )
    users: list[Row] = data.get("value", [])
    if len(users) == 1:
        return users[0], None
    if not users:
        return None, f"Não achei *{query}* no diretório."
    return None, "Encontrei: " + ", ".join(u["displayName"] for u in users) + ". Seja mais específico."


async def build_org(graph: Graph, query: str) -> str:
    if not config.ORG_CHART:
        return NEEDS_SETUP
    try:
        user, err = await find_user(graph, query)
        if err or user is None:
            return err or "Pessoa não encontrada."
        chain: list[Row] = []
        node = user
        for _ in range(MAX_LEVELS):
            boss = await _optional(graph, f"/users/{node['id']}/manager", {"$select": FIELDS})
            if not boss:
                break
            chain.append(boss)
            node = boss
        reports = (await graph.get(f"/users/{user['id']}/directReports", {"$select": FIELDS, "$top": "50"})).get("value", [])
        peers: list[Row] = []
        if chain:
            siblings = await graph.get(f"/users/{chain[0]['id']}/directReports", {"$select": FIELDS, "$top": "50"})
            peers = [p for p in siblings.get("value", []) if p["id"] != user["id"]]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            return NEEDS_SETUP
        raise

    lines = [f"🏢 {_label(user)}"]
    if chain:
        lines.append("**Cadeia acima**\n\n" + "\n".join(f"{'  ' * i}↑ {_label(b)}" for i, b in enumerate(chain)))
    else:
        lines.append("Sem gestor cadastrado no diretório.")
    if reports:
        lines.append(f"**Equipe direta ({len(reports)})**\n\n" + "\n".join(f"- {_label(r)}" for r in reports[:MAX_LIST]))
    if peers:
        lines.append(f"**Mesmo gestor ({len(peers)})**\n\n" + "\n".join(f"- {_label(p)}" for p in peers[:MAX_LIST]))
    if len(reports) > MAX_LIST or len(peers) > MAX_LIST:
        lines.append(f"_(listas limitadas a {MAX_LIST} nomes)_")
    return "\n\n".join(lines)


# ---------- aprendizado a partir do diretório ----------

def _job(u: Row) -> str | None:
    parts = [x for x in (u.get("jobTitle"), u.get("department")) if x]
    return f"Cargo: {parts[0]}" + (f" ({parts[1]})" if len(parts) > 1 else "") if parts else None


async def _chain(graph: Graph, user_id: str) -> list[Row]:
    chain: list[Row] = []
    node_id = user_id
    for _ in range(MAX_LEVELS):
        boss = await _optional(graph, f"/users/{node_id}/manager", {"$select": FIELDS})
        if not boss:
            break
        chain.append(boss)
        node_id = boss["id"]
    return chain


async def sync_org(graph: Graph) -> int:
    """Grava cargo e gestor na ficha das pessoas; seus supervisores (cadeia acima de você) viram prioridade alta."""
    if not config.ORG_CHART:
        return 0
    me = await db.kv_get("me_id") or str((await graph.get("/me"))["id"])
    supervisors = await _chain(graph, me)
    done: set[str] = set()
    for level, boss in enumerate(supervisors):
        person = await people.get_or_create(boss["displayName"], boss["id"])
        role = "Seu gestor direto" if level == 0 else f"Gestor acima de você (nível {level + 1})"
        above = await _optional(graph, f"/users/{boss['id']}/manager", {"$select": FIELDS})
        facts = [role, *filter(None, [_job(boss), f"Gestor: {above['displayName']}" if above else None])]
        await people.set_org_facts(person["id"], facts)
        await learn._apply_priority(person, 1, "supervisor no organograma")
        done.add(boss["id"])
    known = await db.q("SELECT * FROM people WHERE user_id IS NOT NULL AND user_id <> %s", (me,))
    for person in known:
        if person["user_id"] in done:
            continue
        try:
            user = await graph.get(f"/users/{person['user_id']}", {"$select": FIELDS})
            boss = await _optional(graph, f"/users/{person['user_id']}/manager", {"$select": FIELDS})
        except httpx.HTTPStatusError:
            continue  # convidado externo ou conta removida
        facts = list(filter(None, [_job(user), f"Gestor: {boss['displayName']}" if boss else None]))
        await people.set_org_facts(person["id"], facts)
    return len(supervisors)

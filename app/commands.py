"""Lógica de comandos do assistente (sem dependência de transporte): handle, reports, memória."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from . import config, db, learn, llm, people, selfchat
from .people import PRIORITY_LABEL
from .textutil import parse_ids, parse_report_args, split_name_number, split_name_text

log = logging.getLogger("commands")

HELP = """**Comandos**
- `/chats [filtro]` lista seus chats (com número)
- `/acompanhar N` passa a ler o chat N
- `/parar N` para de ler o chat N
- `/monitorados` chats que estou lendo
- `/report [N] [7d]` report de um chat (ou de todos os acompanhados); padrão 1 dia
- `/lembretes` lembretes pendentes · `/cancelar ID`
- `/sync` força sincronização agora

**Pessoas**
- `/pendentes` quem está esperando resposta, por prioridade
- `/pessoas` · `/pessoa Nome` ficha: prioridade, apelidos, notas e onde ela anda ativa
- `/perfil Nome[: pergunta]` quem é + o que está fazendo agora (resumo com IA)
- `/nota Nome: texto` · `/apagar-nota ID`
- `/prioridade Nome 1|2|3` (1 alta, 2 normal, 3 baixa) · `/apelido Nome: apelido`
- `/aprender [auto|revisar|off]` modo de aprendizado (hoje ele aprende sozinho enquanto lê)
- `/aprendido [7d]` o que aprendi · apague o que estiver errado com `/apagar-nota ID`
- `/sugerir [N] [7d]` análise manual de chats, com revisão
- `/sugestoes` · `/aprovar 12 13|todas` · `/rejeitar 12|todas`

No chat de notas, comandos começam com `/`. Para texto livre, comece com `!`: *"!me lembra amanhã às 10 de revisar o PR"*, *"!report do chat 3 da semana"*,
*"!anota que o Rafael é meu gestor, prioridade alta"*,
*"!no que o Cristian está trabalhando?"*."""

_background: set[asyncio.Task[None]] = set()


def now_local() -> datetime:
    return datetime.now(config.TZ)


def run_in_background(job: Callable[[], Awaitable[str]]) -> None:
    """Respostas lentas (LLM) chegam depois, como uma segunda mensagem no chat de notas."""

    async def runner() -> None:
        try:
            text = await job()
        except Exception as exc:
            log.exception("Falha em tarefa de fundo")
            text = f"⚠️ Falhou: {exc}"
        await selfchat.send_to_owner(text)

    task = asyncio.create_task(runner())
    _background.add(task)
    task.add_done_callback(_background.discard)


# ---------- reports ----------

async def _select_chats(chat_num: int | None) -> tuple[list[db.Row], str | None]:
    if chat_num is not None:
        chat = await db.chat_by_num(chat_num)
        if not chat:
            return [], f"Chat {chat_num} não encontrado. Use `/chats`."
        return [chat], None
    chats = await db.monitored_chats()
    if not chats:
        return [], "Nenhum chat acompanhado. Use `/chats` e `/acompanhar N`."
    return chats, None


def _transcript(msgs: list[db.Row]) -> str:
    lines = [
        f"[{m['created_at'].astimezone(config.TZ):%d/%m %H:%M}] {m['author']}: {m['body']}" for m in msgs
    ]
    return "\n".join(lines)[-config.MAX_REPORT_CHARS:]


async def _people_ctx(msgs: list[db.Row]) -> str:
    ids = sorted({m["author_id"] for m in msgs if m.get("author_id")})
    names = sorted({m["author"] for m in msgs})
    return await people.context_for(ids, names)


async def build_report(chat_num: int | None, days: int) -> str:
    chats, err = await _select_chats(chat_num)
    if err:
        return err

    since = now_local() - timedelta(days=days)
    period = f"desde {since:%d/%m %H:%M}"
    parts: list[str] = []
    for chat in chats:
        msgs = await db.messages_since(chat["id"], since)
        if not msgs:
            continue
        summary = await llm.summarize(chat["title"], period, _transcript(msgs), await _people_ctx(msgs))
        parts.append(f"### {chat['title']} ({len(msgs)} msgs)\n\n{summary}")

    if not parts:
        hint = "" if chat_num is None or chats[0]["monitored"] else " (esse chat não está acompanhado)"
        return f"Nada no período ({period}){hint}."
    return "\n\n---\n\n".join(parts)


async def build_suggestions(chat_num: int | None, days: int) -> str:
    chats, err = await _select_chats(chat_num)
    if err:
        return err
    since = now_local() - timedelta(days=days)
    created: list[db.Row] = []
    for chat in chats:
        msgs = await db.messages_since(chat["id"], since)
        if not msgs:
            continue
        items = await llm.suggest_memories(chat["title"], _transcript(msgs), await _people_ctx(msgs))
        authors = {m["author"].lower(): m.get("author_id") for m in msgs}
        created += await people.create_suggestions(items, authors)
    if not created:
        return "Não encontrei nada novo que valha guardar. 🙂"
    return (
        "🧠 **Sugestões de memória**\n\n"
        + "\n\n".join(people.format_suggestion(s) for s in created)
        + f"\n\nAprovar: `/aprovar {' '.join(str(c['id']) for c in created)}` (ou só alguns IDs) ou "
        "`/aprovar todas` · Rejeitar: `/rejeitar ...`"
    )


async def build_pending() -> str:
    rows = await people.pending_replies()
    if not rows:
        if not await db.kv_get("me_id"):
            return "Ainda não sei qual é o seu usuário. Aguarde o worker sincronizar (`/sync`)."
        return "✅ Nada esperando resposta nos chats acompanhados."
    out = ["**Esperando sua resposta** (ordem: prioridade, depois mais antigo)"]
    for r in rows:
        since = r["since"].astimezone(config.TZ)
        out.append(
            f"{PRIORITY_LABEL[r['priority']].split()[0]} `{r['num']}` **{r['title']}** · {r['n']} msg desde "
            f"{since:%d/%m %H:%M}\n> {r['last']}"
        )
    return "\n\n".join(out)


PERSON_DAYS = 14
PERSON_QUESTION = ""  # vazio = perfil completo (quem é + fazendo agora)


async def build_person(query: str, question: str) -> str:
    person, err = await people.resolve(query)
    if err or person is None:
        return err or "Pessoa não encontrada."
    since = now_local() - timedelta(days=PERSON_DAYS)
    msgs = await people.recent_messages(person, since)
    transcript = "\n".join(
        f"[{m['created_at'].astimezone(config.TZ):%d/%m %H:%M}] ({m['title']}) {m['body']}" for m in msgs
    )[-config.MAX_REPORT_CHARS:]
    card = await people.card(person)
    if not msgs and "Sem notas ainda." in card:
        return f"Ainda não sei nada sobre **{person['name']}**. Acompanhe um chat com ela (`/acompanhar N`)."
    answer = await llm.describe_person(question, card, transcript, profile=not question)
    if question:
        return answer
    rows = await people.activity(person, since)
    last = f" · última msg {rows[0]['last'].astimezone(config.TZ):%d/%m %H:%M}" if rows else ""
    aliases = f" · apelidos: {', '.join(person['aliases'])}" if person["aliases"] else ""
    return f"**{person['name']}** · prioridade {PRIORITY_LABEL[person['priority']]}{aliases}{last}\n\n{answer}"


async def remember(name: str, fact: str | None, priority: int | None) -> str:
    person, err = await people.resolve(name, create=True)
    if err or person is None:
        return err or "Pessoa não encontrada."
    done = []
    if fact:
        fid = await people.add_fact(person["id"], fact)
        done.append(f"nota `{fid}`: {fact}")
    if priority in (1, 2, 3):
        await people.set_priority(person["id"], priority)
        done.append(f"prioridade {PRIORITY_LABEL[priority]}")
    if not done:
        return "Não entendi o que guardar."
    return f"🧠 **{person['name']}** — " + " · ".join(done)


# ---------- comandos ----------

async def handle(text: str) -> str:
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    cmd = {"/sugestões": "/sugestoes", "/apagar_nota": "/apagar-nota"}.get(cmd, cmd)

    if cmd in ("/ajuda", "/help", "ajuda", "help", "oi", "olá", "ola"):
        return HELP

    if cmd == "/chats":
        rows = await db.list_chats(arg or None)
        if not rows:
            await db.kv_set("sync_requested", "1")
            return "Nenhum chat ainda. Pedi uma sincronização; tente de novo em ~1 min."
        lines = [f"`{r['num']}` {'👁️ ' if r['monitored'] else ''}{r['title']}" for r in rows]
        return "**Seus chats (mais recentes):**\n\n" + "\n\n".join(lines)

    if cmd in ("/acompanhar", "/parar"):
        if not arg.isdigit():
            return f"Uso: `{cmd} N` (veja os números em `/chats`)"
        row = await db.set_monitored(int(arg), cmd == "/acompanhar")
        if not row:
            return f"Chat {arg} não encontrado."
        if cmd == "/acompanhar":
            await db.kv_set("sync_requested", "1")
            return f"👁️ Acompanhando **{row['title']}**. Vou puxar os últimos {config.BACKFILL_DAYS} dias."
        return f"Parei de acompanhar **{row['title']}**."

    if cmd == "/monitorados":
        rows = await db.monitored_chats()
        if not rows:
            return "Nenhum chat acompanhado."
        return "\n\n".join(f"`{r['num']}` {r['title']}" for r in rows)

    if cmd == "/report":
        chat_num, days = parse_report_args(arg)
        run_in_background(lambda: build_report(chat_num, days))
        return "⏳ Gerando report..."

    if cmd == "/lembretes":
        rows = await db.pending_reminders()
        if not rows:
            return "Nenhum lembrete pendente."
        return "\n\n".join(
            f"`#{r['id']}` {r['due_at'].astimezone(config.TZ):%d/%m %H:%M} — {r['text']}" for r in rows
        )

    if cmd == "/cancelar":
        if not arg.lstrip("#").isdigit():
            return "Uso: `/cancelar ID`"
        ok = await db.cancel_reminder(int(arg.lstrip("#")))
        return "🗑️ Cancelado." if ok else "Lembrete não encontrado (ou já enviado)."

    if cmd == "/pendentes":
        return await build_pending()

    if cmd == "/pessoas":
        rows = await people.list_people()
        if not rows:
            return "Nenhuma pessoa ainda. Use `/nota Nome: texto` ou `/sugerir`."
        return "\n\n".join(
            f"{PRIORITY_LABEL[r['priority']].split()[0]} **{r['name']}** · {r['facts']} nota(s)" for r in rows
        )

    if cmd == "/perfil":
        name, _, question = arg.partition(":")
        if not name.strip():
            return "Uso: `/perfil Nome` ou `/perfil Nome: no que está trabalhando?`"
        run_in_background(lambda: build_person(name.strip(), question.strip() or PERSON_QUESTION))
        return "⏳ Montando o perfil..."

    if cmd == "/pessoa":
        person, err = await people.resolve(arg)
        if err or person is None:
            return err
        rows = await people.activity(person, now_local() - timedelta(days=PERSON_DAYS))
        return (
            await people.card(person)
            + f"\n\n**Atividade ({PERSON_DAYS}d)**\n\n{people.activity_lines(rows)}"
            + f"\n\nResumo com IA: `/perfil {person['name']}`"
        )

    if cmd == "/nota":
        name, fact = split_name_text(arg)
        if not name or not fact:
            return "Uso: `/nota Nome: texto`"
        return await remember(name, fact, None)

    if cmd == "/apagar-nota":
        if not arg.isdigit():
            return "Uso: `/apagar-nota ID` (veja os IDs em `/pessoa Nome`)"
        return "🗑️ Nota apagada." if await people.delete_fact(int(arg)) else "Nota não encontrada."

    if cmd == "/prioridade":
        name, prio = split_name_number(arg)
        if not name or prio not in (1, 2, 3):
            return "Uso: `/prioridade Nome 1|2|3` (1 alta, 2 normal, 3 baixa)"
        return await remember(name, None, prio)

    if cmd == "/apelido":
        name, alias = split_name_text(arg)
        person, err = await people.resolve(name)
        if err or person is None or not alias:
            return err or "Uso: `/apelido Nome: apelido`"
        await people.add_alias(person["id"], alias)
        return f"**{person['name']}** agora também é *{alias}*."

    if cmd == "/sugerir":
        chat_num, days = parse_report_args(arg)
        if "d" not in arg.lower():
            days = 7
        run_in_background(lambda: build_suggestions(chat_num, days))
        return "⏳ Lendo os chats para sugerir memórias..."

    if cmd == "/aprender":
        if arg.lower() in learn.MODES:
            await db.kv_set("learn_mode", arg.lower())
        current = await learn.mode()
        desc = {
            "auto": "aprendo sozinho e te mostro o resumo (`/aprendido`)",
            "review": "só sugiro; nada entra sem `/aprovar`",
            "off": "desligado",
        }[current]
        return f"🧠 Modo de aprendizado: **{ {'review': 'revisar'}.get(current, current) }** — {desc}.\nTroque com `/aprender auto|revisar|off`."

    if cmd == "/aprendido":
        _, days = parse_report_args(arg)
        text = await learn.digest(now_local() - timedelta(days=days))
        return text or "Nada aprendido nesse período."

    if cmd == "/sugestoes":
        rows = await people.pending_suggestions()
        return "\n\n".join(people.format_suggestion(r) for r in rows) if rows else "Nenhuma sugestão pendente."

    if cmd in ("/aprovar", "/rejeitar"):
        ids = parse_ids(arg)
        if ids == []:
            return f"Uso: `{cmd} 12 13` ou `{cmd} todas`"
        n = await people.review(ids, approve=cmd == "/aprovar")
        return f"{'✅ Aprovadas' if cmd == '/aprovar' else '🗑️ Rejeitadas'}: {n}."

    if cmd == "/sync":
        await db.kv_set("sync_requested", "1")
        return "🔄 Sincronização pedida."

    return await handle_free_text(text)


async def handle_free_text(text: str) -> str:
    now = now_local()
    intent = await llm.parse_intent(text, now)

    if intent.get("type") == "reminder":
        try:
            due = datetime.fromisoformat(str(intent["due"]))
        except (KeyError, ValueError):
            return "Não entendi quando. Tente: *me lembra amanhã às 10h de X*."
        if due.tzinfo is None:
            due = due.replace(tzinfo=config.TZ)
        if due <= now:
            return f"Esse horário ({due:%d/%m %H:%M}) já passou. Pode repetir com a data?"
        what = str(intent.get("text") or text)
        rid = await db.add_reminder(what, due)
        return f"✅ Lembrete `#{rid}` para **{due.astimezone(config.TZ):%d/%m %H:%M}**: {what}"

    if intent.get("type") == "report":
        chat = intent.get("chat")
        days = int(intent.get("days") or 1)
        chat_num = int(chat) if isinstance(chat, int | str) and str(chat).isdigit() else None
        run_in_background(lambda: build_report(chat_num, days))
        return "⏳ Gerando report..."

    if intent.get("type") == "person" and intent.get("person"):
        who, question = str(intent["person"]), str(intent.get("question") or PERSON_QUESTION)
        run_in_background(lambda: build_person(who, question))
        return "⏳ Montando o perfil..."

    if intent.get("type") == "memory" and intent.get("person"):
        prio = intent.get("priority")
        return await remember(
            str(intent["person"]), intent.get("fact") or None, prio if prio in (1, 2, 3) else None
        )

    return "Não entendi. 🤔\n\n" + HELP

"""Agendador: lembretes, report diário (+ "O que aprendi") e alertas de prioridade."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta

from . import config, db, learn, people
from .commands import build_report, now_local
from .graph import NotLoggedInError
from .people import PRIORITY_LABEL
from .selfchat import notify_email, send_to_owner

log = logging.getLogger("scheduler")


async def _daily_report_due(now: datetime) -> bool:
    if not config.DAILY_REPORT_TIME:
        return False
    hh, mm = (int(x) for x in config.DAILY_REPORT_TIME.split(":"))
    today = now.date().isoformat()
    if now.time() < time(hh, mm) or await db.kv_get("daily_report") == today:
        return False
    await db.kv_set("daily_report", today)
    return True


async def scheduler() -> None:
    while True:
        try:
            for r in await db.due_reminders():
                await send_to_owner(f"⏰ **Lembrete:** {r['text']}")
                await notify_email("Lembrete", f"⏰ **Lembrete:** {r['text']}")
                await db.mark_sent(r["id"])

            if await _daily_report_due(now_local()):
                report = await build_report(None, 1)
                await send_to_owner("📋 **Report diário**\n\n" + report)
                now = now_local()
                raw = await db.kv_get("learn_digest_ts")
                since = datetime.fromisoformat(raw) if raw else now - timedelta(days=1)
                await db.kv_set("learn_digest_ts", now.isoformat())
                learned = await learn.digest(since)
                if learned:
                    await send_to_owner(learned)

            if config.ALERT_PRIORITY:
                alerts = await people.priority_alerts()
                if alerts:
                    lines = [
                        f"{PRIORITY_LABEL[a['priority']].split()[0]} **{a['name']}** em `{a['num']}` "
                        f"{a['title']}: {a['body'][:200]}"
                        for a in alerts
                    ]
                    text = "🔔 **Mensagem de pessoa prioritária**\n\n" + "\n\n".join(lines)
                    await send_to_owner(text)
                    await notify_email("Mensagem de pessoa prioritária", text)

            status = await db.kv_get("worker_status")
            alerted = await db.kv_get("login_alert") == "1"
            if status == "login_required" and not alerted:
                log.warning(
                    "Perdi o acesso aos seus chats. No console do container worker, rode `python -m app.login`."
                )
                await db.kv_set("login_alert", "1")
            elif status == "ok" and alerted:
                await db.kv_set("login_alert", "0")
        except NotLoggedInError as exc:
            log.warning("Sem login (%s); agendador aguardando o `python -m app.login`.", exc)
        except Exception:
            log.exception("Erro no agendador")
        await asyncio.sleep(30)

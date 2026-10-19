"""Processo único: sincronização do Graph, leitura de comandos do chat de notas e agendador."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import commands, db, selfchat
from .graph import Graph, TokenProvider
from .scheduler import scheduler
from .worker import sync_loop

log = logging.getLogger("main")


async def supervise(name: str, job: Callable[[], Awaitable[None]]) -> None:
    """Uma tarefa que cai é reiniciada com backoff e não derruba as outras."""
    delay = 5
    while True:
        try:
            await job()
        except Exception:
            log.exception("Tarefa %s caiu; reiniciando em %ss", name, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.init()
    graph = Graph(TokenProvider())
    selfchat.bind(graph)
    await asyncio.gather(
        supervise("sync", lambda: sync_loop(graph)),
        supervise("comandos", lambda: selfchat.poll_commands(graph, commands.handle, commands.handle_free_text)),
        supervise("agendador", scheduler),
    )


if __name__ == "__main__":
    asyncio.run(main())

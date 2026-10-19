import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app import commands


def _patch(monkeypatch: pytest.MonkeyPatch, msgs: list[dict[str, Any]], card: str) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    async def resolve(q: str, create: bool = False) -> tuple[Any, str | None]:
        return {"id": 1, "name": "Cristian", "user_id": "u1"}, None

    async def recent(person: Any, since: Any, limit: int = 300) -> list[dict[str, Any]]:
        return msgs

    async def get_card(person: Any) -> str:
        return card

    async def describe(question: str, card: str, transcript: str) -> str:
        calls.append((question, card, transcript))
        return "resposta"

    monkeypatch.setattr(commands.people, "resolve", resolve)
    monkeypatch.setattr(commands.people, "recent_messages", recent)
    monkeypatch.setattr(commands.people, "card", get_card)
    monkeypatch.setattr(commands.llm, "describe_person", describe)
    return calls


def test_build_person_sends_card_and_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    msg = {"created_at": datetime(2026, 10, 19, 15, tzinfo=timezone.utc), "body": "fechei a API", "title": "Daily"}
    calls = _patch(monkeypatch, [msg], "**Cristian**\n\n`1` dev backend")
    assert asyncio.run(commands.build_person("cris", "no que trabalha?")) == "resposta"
    question, card, transcript = calls[0]
    assert question == "no que trabalha?" and "dev backend" in card and "(Daily) fechei a API" in transcript


def test_build_person_without_data(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, [], "**Cristian**\n\nSem notas ainda.")
    assert "Ainda não sei nada" in asyncio.run(commands.build_person("cris", "x"))
    assert calls == []


def test_perfil_command_runs_in_background(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Any] = []
    monkeypatch.setattr(commands, "run_in_background", started.append)
    assert "Montando" in asyncio.run(commands.handle("/perfil Cristian: no que trabalha?"))
    assert len(started) == 1
    assert "Uso:" in asyncio.run(commands.handle("/perfil"))

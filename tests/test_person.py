import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app import commands


def _patch(monkeypatch: pytest.MonkeyPatch, msgs: list[dict[str, Any]], card: str) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    async def resolve(q: str, create: bool = False) -> tuple[Any, str | None]:
        return {"id": 1, "name": "Cristian", "user_id": "u1", "aliases": ["Cris"], "priority": 1}, None

    async def recent(person: Any, since: Any, limit: int = 300) -> list[dict[str, Any]]:
        return msgs

    async def get_card(person: Any) -> str:
        return card

    async def describe(question: str, card: str, transcript: str, profile: bool = False) -> str:
        calls.append((question, card, transcript))
        return "resposta"

    async def activity(person: Any, since: Any) -> list[dict[str, Any]]:
        return [{"num": 7, "title": "Daily", "n": 3, "last": datetime(2026, 10, 19, 15, tzinfo=timezone.utc)}]

    monkeypatch.setattr(commands.people, "activity", activity)

    monkeypatch.setattr(commands.people, "resolve", resolve)
    monkeypatch.setattr(commands.people, "recent_messages", recent)
    monkeypatch.setattr(commands.people, "card", get_card)
    monkeypatch.setattr(commands.llm, "describe_person", describe)
    return calls


def test_build_person_sends_card_and_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    msg = {"created_at": datetime(2026, 10, 19, 15, tzinfo=timezone.utc), "body": "fechei a API", "title": "Daily"}
    calls = _patch(monkeypatch, [msg], "**Cristian**\n\n`1` dev backend")
    assert asyncio.run(commands.build_person("cris", "no que trabalha?")) == "resposta"  # pergunta: sem cabeçalho
    question, card, transcript = calls[0]
    assert question == "no que trabalha?" and "dev backend" in card and "(Daily) fechei a API" in transcript


def test_profile_has_header_with_priority_and_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    msg = {"created_at": datetime(2026, 10, 19, 15, tzinfo=timezone.utc), "body": "oi", "title": "Daily"}
    _patch(monkeypatch, [msg], "ficha")
    out = asyncio.run(commands.build_person("cris", ""))
    assert out.startswith("**Cristian** · ") and "prioridade" in out and "apelidos: Cris" in out and "última msg 19/10" in out
    assert out.endswith("resposta")


def test_pessoa_shows_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [], "**Cristian**")
    out = asyncio.run(commands.handle("/pessoa cris"))
    assert "**Atividade (14d)**" in out and "`7` Daily · 3 msg" in out and "/perfil Cristian" in out


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

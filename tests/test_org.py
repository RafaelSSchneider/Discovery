import asyncio
from typing import Any

import httpx
import pytest

from app import config, org
from app.graph import Graph


class FakeTokens:
    def get_token(self) -> str:
        return "tok"

    def reload(self) -> None:
        pass


def u(i: str, name: str, title: str = "") -> dict[str, Any]:
    return {"id": i, "displayName": name, "jobTitle": title, "department": "TI"}


TREE = {
    "/users/me": u("me", "Rafael", "Dev"),
    "/users/me/manager": u("m1", "Ana", "Gerente"),
    "/users/m1/manager": u("d1", "Bia", "Diretora"),
    "/users/d1/manager": None,
    "/users/me/directReports": {"value": [u("r1", "Caio", "Estagiário")]},
    "/users/m1/directReports": {"value": [u("me", "Rafael"), u("p1", "Davi", "Dev")]},
}


def graph() -> Graph:
    def handler(req: httpx.Request) -> httpx.Response:
        data = TREE.get(req.url.path.removeprefix("/v1.0"))
        if data is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=data)

    return Graph(FakeTokens(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_org_chart(monkeypatch: pytest.MonkeyPatch) -> None:
    async def kv_get(key: str) -> str | None:
        return "me"

    monkeypatch.setattr(config, "ORG_CHART", True)
    monkeypatch.setattr(org.db, "kv_get", kv_get)
    out = asyncio.run(org.build_org(graph(), ""))
    assert "**Rafael** · Dev · TI" in out
    assert "↑ **Ana** · Gerente" in out and "  ↑ **Bia** · Diretora" in out
    assert "Equipe direta (1)" in out and "Caio" in out
    assert "Mesmo gestor (1)" in out and "Davi" in out and out.count("Rafael") == 1


def test_org_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ORG_CHART", False)
    assert "ORG_CHART=true" in asyncio.run(org.build_org(graph(), ""))


def test_org_forbidden_gives_setup_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ORG_CHART", True)
    g = Graph(FakeTokens(), transport=httpx.MockTransport(lambda r: httpx.Response(403, json={})))  # type: ignore[arg-type]
    assert "User.Read.All" in asyncio.run(org.build_org(g, "ana@x.com"))

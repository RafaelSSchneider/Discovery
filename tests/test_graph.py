import asyncio

import httpx
import pytest

from app import graph as graph_mod
from app.graph import Graph, NotLoggedInError


class FakeTokens:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.reloads = 0

    def get_token(self) -> str:
        if self.fail_first and self.reloads == 0:
            raise NotLoggedInError("sem conta")
        return "tok"

    def reload(self) -> None:
        self.reloads += 1


def test_post_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(201, json={"id": "1"})

    g = Graph(FakeTokens(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    out = asyncio.run(g.post("/chats/48:notes/messages", {"body": {"content": "x"}}))
    assert out == {"id": "1"} and len(calls) == 2
    assert calls[1].headers["Authorization"] == "Bearer tok"


def test_not_logged_in_triggers_reload() -> None:
    tokens = FakeTokens(fail_first=True)
    g = Graph(tokens, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": 1})))  # type: ignore[arg-type]
    assert asyncio.run(g.get("/me")) == {"ok": 1}
    assert tokens.reloads == 1


def test_scopes_have_no_offline_access() -> None:
    assert "offline_access" not in graph_mod.SCOPES
    assert {"User.Read", "Chat.Read", "ChatMessage.Send"} <= set(graph_mod.SCOPES)

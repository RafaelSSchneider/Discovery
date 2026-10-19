import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app import config, selfchat
from app.graph import Graph
from app.selfchat import md_to_html, split_message

ME = "me-id"


def test_md_to_html_formats_and_escapes() -> None:
    out = md_to_html("**Oi** <script>alert(1)</script> & `a<b` *it*\n- item\n### Título\n> citação")
    assert out == (
        "<strong>Oi</strong> &lt;script&gt;alert(1)&lt;/script&gt; &amp; <code>a&lt;b</code> <em>it</em>"
        "<br>• item<br><strong>Título</strong><br>│ citação"
    )


def test_md_to_html_code_is_not_formatted() -> None:
    assert md_to_html("`**x**`") == "<code>**x**</code>"


def test_split_message() -> None:
    assert split_message("curto") == ["curto"]
    text = "\n\n".join(["a" * 50] * 5)
    chunks = split_message(text, 120)
    assert all(len(c) <= 120 for c in chunks) and len(chunks) == 3
    assert "\n\n".join(chunks) == text
    long = split_message("x" * 250, 100)
    assert [len(c) for c in long] == [100, 100, 50]


class FakeKV:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, data: dict[str, str]) -> None:
        self.data = data

        async def get(key: str) -> str | None:
            return self.data.get(key)

        async def set_(key: str, value: str) -> None:
            self.data[key] = value

        monkeypatch.setattr(selfchat.db, "kv_get", get)
        monkeypatch.setattr(selfchat.db, "kv_set", set_)


class FakeTokens:
    def get_token(self) -> str:
        return "tok"

    def reload(self) -> None:
        pass


def msg(mid: str, text: str, created: datetime, sender: str = ME) -> dict[str, Any]:
    return {
        "id": mid,
        "messageType": "message",
        "createdDateTime": created.isoformat(),
        "from": {"user": {"id": sender}},
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
    }


class Chat:
    """Graph falso do chat de notas: GET devolve `messages`; POST guarda o enviado em `sent`."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.sent: list[str] = []
        self.graph = Graph(FakeTokens(), transport=httpx.MockTransport(self.handler))  # type: ignore[arg-type]

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            self.sent.append(json.loads(req.content)["body"]["content"])
            return httpx.Response(201, json={"id": "new"})
        return httpx.Response(200, json={"value": self.messages})


def run_poll(chat: Chat, ran: list[str]) -> int:
    async def handle(text: str) -> str:
        ran.append(text)
        return f"ok {text}"

    async def free(text: str) -> str:
        ran.append("FREE:" + text)
        return "livre"

    return asyncio.run(selfchat.poll_once(chat.graph, handle, free))


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
WM = (NOW - timedelta(minutes=10)).isoformat()


def test_filters_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeKV(monkeypatch, {"me_id": ME, "cmd_watermark": WM})
    t = NOW - timedelta(minutes=5)
    chat = Chat([
        msg("1", "/ajuda", t),
        msg("2", "/chats", t + timedelta(seconds=1), sender="outra-pessoa"),
        msg("3", "🤖 /ajuda", t + timedelta(seconds=2)),
        msg("4", "só uma nota", t + timedelta(seconds=3)),
        msg("5", "!me lembra de X", t + timedelta(seconds=4)),
        msg("6", "!", t + timedelta(seconds=5)),
    ])
    ran: list[str] = []
    assert run_poll(chat, ran) == 2
    assert ran == ["/ajuda", "FREE:me lembra de X"]
    assert len(chat.sent) == 2 and all(s.startswith("🤖 ") for s in chat.sent)


def test_watermark_and_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    kv = FakeKV(monkeypatch, {"me_id": ME, "cmd_watermark": WM})
    t = NOW - timedelta(minutes=5)
    chat = Chat([msg("1", "/ajuda", t), msg("old", "/sync", NOW - timedelta(hours=1))])
    ran: list[str] = []
    assert run_poll(chat, ran) == 1
    assert kv.data["cmd_watermark"] == t.isoformat()
    assert run_poll(chat, ran) == 0  # mesma mensagem volta na lista: não reexecuta
    chat.messages.insert(0, msg("2", "/sync", t))  # mesmo timestamp, id novo
    assert run_poll(chat, ran) == 1
    assert ran == ["/ajuda", "/sync"]


def test_first_run_skips_old_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    kv = FakeKV(monkeypatch, {"me_id": ME})
    chat = Chat([msg("1", "/apagar-nota 1", NOW - timedelta(days=2))])
    ran: list[str] = []
    assert run_poll(chat, ran) == 0 and ran == []
    assert "cmd_watermark" in kv.data and not chat.sent


def test_failing_command_replies_and_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeKV(monkeypatch, {"me_id": ME, "cmd_watermark": WM})
    chat = Chat([msg("1", "/boom", NOW - timedelta(minutes=5))])

    async def boom(text: str) -> str:
        raise ValueError("falhou")

    assert asyncio.run(selfchat.poll_once(chat.graph, boom, boom)) == 1
    assert "Erro interno" in chat.sent[0]
    assert asyncio.run(selfchat.poll_once(chat.graph, boom, boom)) == 0


def test_notes_chat_404_sets_status_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    kv = FakeKV(monkeypatch, {"me_id": ME, "cmd_watermark": WM})
    graph = Graph(FakeTokens(), transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))  # type: ignore[arg-type]
    sleeps: list[float] = []

    async def stop_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(selfchat.asyncio, "sleep", stop_sleep)

    async def noop(text: str) -> str:
        return ""

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(selfchat.poll_commands(graph, noop, noop))
    assert "404" in kv.data["selfchat_status"] and sleeps == [selfchat.NOT_FOUND_BACKOFF]
    assert any("48:notes" in r.message for r in caplog.records)


def test_send_to_owner_posts_html_with_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = Chat([])
    selfchat.bind(chat.graph)
    asyncio.run(selfchat.send_to_owner("**oi** <b>"))
    assert chat.sent == ["🤖 <strong>oi</strong> &lt;b&gt;"]
    assert config.NOTES_CHAT_ID == "48:notes"

import asyncio
import json

import httpx
import pytest

from app import config, llm


def test_openai_compatible_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"choices": [{"message": {"content": " oi "}}]})

    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://x.test/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(llm.complete("sys", "user", "m1")) == "oi"
    assert str(seen[0].url) == "https://x.test/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer k"
    body = json.loads(seen[0].content)
    assert body["model"] == "m1" and body["messages"][0] == {"role": "system", "content": "sys"}


def test_extract_json_tolerates_noise() -> None:
    assert llm.extract_json('Claro! ```json\n{"type": "person"}\n``` pronto') == '{"type": "person"}'
    assert llm.extract_json('texto [{"a": 1}] fim', "[", "]") == '[{"a": 1}]'
    assert llm.extract_json("sem json") == "sem json"


def test_openai_retries_without_system_role_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": "Developer instruction is not enabled"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(llm.complete("sys", "user", "gemma", max_tokens=300)) == "ok"
    assert bodies[1]["messages"] == [{"role": "user", "content": "sys\n\nuser"}]
    assert bodies[1]["max_tokens"] == 4000


def test_openai_error_includes_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    transport = httpx.MockTransport(lambda r: httpx.Response(429, text="rate limited"))
    monkeypatch.setattr(llm, "_http", httpx.AsyncClient(transport=transport))
    with pytest.raises(RuntimeError, match="429.*rate limited"):
        asyncio.run(llm.complete("s", "u", "m"))

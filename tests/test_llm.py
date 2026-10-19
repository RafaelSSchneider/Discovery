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

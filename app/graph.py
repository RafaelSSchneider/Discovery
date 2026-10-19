"""Microsoft Graph com permissões delegadas (lê como a SUA conta)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import msal

from . import config

log = logging.getLogger(__name__)

# offline_access/openid/profile são adicionados automaticamente pelo MSAL
SCOPES = ["User.Read", "Chat.Read"]


class NotLoggedInError(RuntimeError):
    """Não há token válido em cache: rode `python -m app.login`."""


class TokenProvider:
    def __init__(self, cache_path: str = config.TOKEN_CACHE_PATH) -> None:
        self.cache_path = Path(cache_path)
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        self.app = msal.PublicClientApplication(
            config.GRAPH_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{config.GRAPH_TENANT_ID}",
            token_cache=self.cache,
        )

    def _persist(self) -> None:
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(self.cache.serialize())
            os.replace(tmp, self.cache_path)

    def get_token(self) -> str:
        accounts = self.app.get_accounts()
        if not accounts:
            raise NotLoggedInError("nenhuma conta no cache")
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
        self._persist()
        if not result or "access_token" not in result:
            raise NotLoggedInError(str((result or {}).get("error_description", "token expirado")))
        return str(result["access_token"])

    def device_login(self) -> dict[str, Any]:
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Falha ao iniciar device flow: {flow}")
        print(flow["message"], flush=True)
        result: dict[str, Any] = self.app.acquire_token_by_device_flow(flow)
        self._persist()
        return result


class Graph:
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, tokens: TokenProvider) -> None:
        self.tokens = tokens
        self.http = httpx.AsyncClient(base_url=self.BASE, timeout=60)

    async def get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        resp: httpx.Response | None = None
        for attempt in range(5):
            token = await asyncio.to_thread(self.tokens.get_token)
            resp = await self.http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code in (429, 503, 504):
                wait = int(resp.headers.get("Retry-After", 5 * 2**attempt))
                log.warning("Graph %s em %s, aguardando %ss", resp.status_code, url, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        assert resp is not None
        resp.raise_for_status()
        return {}

    async def paged(
        self, url: str, params: dict[str, str] | None = None, max_pages: int = 50
    ) -> AsyncIterator[dict[str, Any]]:
        data = await self.get(url, params)
        pages = 1
        while True:
            for item in data.get("value", []):
                yield item
            next_link = data.get("@odata.nextLink")
            if not next_link or pages >= max_pages:
                return
            data = await self.get(next_link)  # nextLink já traz os parâmetros
            pages += 1

    async def aclose(self) -> None:
        await self.http.aclose()

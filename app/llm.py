"""Chamadas ao LLM: interpretar pedidos e gerar resumos."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from . import config

log = logging.getLogger(__name__)
_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY or None)


_http = httpx.AsyncClient(timeout=120)


async def _complete_openai(system: str, user: str, model: str, max_tokens: int) -> str:
    resp = await _http.post(
        f"{config.LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
    )
    resp.raise_for_status()
    return str(resp.json()["choices"][0]["message"]["content"] or "").strip()


async def complete(system: str, user: str, model: str, max_tokens: int = 2000) -> str:
    if config.LLM_PROVIDER == "openai":
        return await _complete_openai(system, user, model, max_tokens)
    resp = await _client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


INTENT_SYSTEM = """Você interpreta mensagens enviadas a um assistente pessoal no Teams.
Responda SOMENTE com um objeto JSON, sem markdown, em um destes formatos:

{"type": "reminder", "text": "<o que lembrar, curto>", "due": "<ISO 8601 com offset>"}
{"type": "report", "chat": <número do chat ou null>, "days": <inteiro, padrão 1>}
{"type": "memory", "person": "<nome>", "fact": "<fato curto ou null>", "priority": <1, 2, 3 ou null>}
{"type": "person", "person": "<nome ou apelido>", "question": "<a pergunta, ex.: no que ele está trabalhando>"}
{"type": "unknown"}

Regras:
- "me lembra/avisa ..." => reminder. Resolva datas relativas a partir de AGORA.
  Sem horário explícito: use 09:00. "daqui a X" soma ao horário atual.
- Pedido de resumo/report/o que rolou => report.
- Informação sobre uma PESSOA para guardar ("lembre que o Rafael é meu gestor", "anota que a Ana
  aprova os deploys", "Bruno é prioridade alta") => memory. Prioridade: 1 alta, 2 normal, 3 baixa.
  Diferença para reminder: memory é um fato sobre alguém, sem data para avisar.
- Pergunta sobre uma pessoa ("quem é o Cristian", "no que a Ana está trabalhando", "como o Bruno prefere
  receber pedidos") => person. Diferença para memory: person PERGUNTA, memory GUARDA uma informação.
- Qualquer outra coisa => unknown."""


async def parse_intent(text: str, now: datetime) -> dict[str, Any]:
    user = f"AGORA: {now.isoformat()} ({now:%A})\nMENSAGEM: {text}"
    try:
        raw = await complete(INTENT_SYSTEM, user, config.LLM_FAST_MODEL, max_tokens=300)
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data: dict[str, Any] = json.loads(raw)
        return data
    except Exception:
        log.exception("Falha ao interpretar intenção")
        return {"type": "unknown"}


SUMMARY_SYSTEM = """Você gera reports objetivos de conversas de trabalho do Microsoft Teams, em português.
Formato (markdown simples, sem títulos grandes):
- **Resumo:** 2-4 frases.
- **Decisões:** lista (omita se não houver).
- **Pendências / quem ficou de fazer o quê:** lista com responsável (omita se não houver).
- **Responder primeiro:** o que aguarda resposta de {owner}, ordenado pela prioridade das pessoas
  (contexto abaixo; quem não aparece lá é prioridade normal). Omita se não houver.
Use o contexto das pessoas para interpretar papéis, mas não o repita no report.
Seja factual; não invente nada que não esteja nas mensagens."""


async def summarize(chat_title: str, period: str, transcript: str, people_ctx: str = "") -> str:
    owner = config.OWNER_NAME or "o usuário"
    system = SUMMARY_SYSTEM.format(owner=owner)
    user = (
        f"Chat: {chat_title}\nPeríodo: {period}\n\n"
        f"Pessoas conhecidas:\n{people_ctx or '(nenhuma)'}\n\nMensagens:\n{transcript}"
    )
    return await complete(system, user, config.LLM_MODEL, max_tokens=1500)


SUGGEST_SYSTEM = """Você ajuda {owner} a manter memórias de trabalho sobre as pessoas com quem conversa no Teams.
A partir das mensagens, sugira fatos ÚTEIS e DURÁVEIS sobre cada pessoa: papel/cargo, responsabilidades,
projetos e no que está trabalhando agora, quem decide ou aprova o quê, estilo de trabalho e de comunicação
(direto, detalhista, formal, informal...), prazos recorrentes. Se os outros ou a própria pessoa usam um
apelido para ela, informe em "alias".
Sugira prioridade de resposta (1 alta, 2 normal, 3 baixa) só com sinal claro
(ex.: gestor de {owner}, cliente, quem cobra prazos, alguém que raramente precisa de resposta).

NÃO sugira: dados pessoais ou sensíveis (saúde, família, religião, política, finanças pessoais, vida íntima),
fofoca, julgamentos sobre a personalidade, nem nada sobre o próprio {owner}.
Só inclua o que estiver claro nas mensagens (dito pela própria pessoa ou de forma explícita por outros);
na dúvida, omita. Não repita o que já está nas pessoas conhecidas e não reescreva o mesmo fato com outras palavras.
Use o nome exatamente como aparece nas mensagens.

Responda SOMENTE com JSON, sem markdown:
[{{"person": "...", "fact": "... ou null", "alias": "apelido ou null", "priority": 1|2|3|null, "why": "motivo curto"}}]
ou [] se não houver nada bom. Máximo 8 itens."""


async def suggest_memories(chat_title: str, transcript: str, people_ctx: str) -> list[dict[str, Any]]:
    owner = config.OWNER_NAME or "o usuário"
    user = f"Chat: {chat_title}\n\nPessoas conhecidas:\n{people_ctx or '(nenhuma)'}\n\nMensagens:\n{transcript}"
    raw = await complete(SUGGEST_SYSTEM.format(owner=owner), user, config.LLM_MODEL, max_tokens=1500)
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Sugestões em formato inválido: %s", raw[:300])
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


PERSON_SYSTEM = """Você responde perguntas de {owner} sobre uma pessoa com quem trabalha, em português, de forma direta.
Use SOMENTE a ficha e as mensagens recentes dela abaixo. Cite o chat ou a data quando ajudar.
Se não houver informação suficiente, diga o que falta em vez de inventar.
Trabalho, papel, estilo de comunicação e como lidar com a pessoa são bem-vindos. Não especule sobre
vida pessoal, saúde, família ou outros temas sensíveis, mesmo que as mensagens os mencionem."""


async def describe_person(question: str, card: str, transcript: str) -> str:
    owner = config.OWNER_NAME or "o usuário"
    user = f"Pergunta: {question}\n\nFicha:\n{card}\n\nMensagens recentes da pessoa:\n{transcript or '(nenhuma)'}"
    return await complete(PERSON_SYSTEM.format(owner=owner), user, config.LLM_MODEL, max_tokens=1200)

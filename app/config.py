"""Configuração via variáveis de ambiente."""

import os
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


OWNER_NAME = _env("OWNER_NAME")

GRAPH_CLIENT_ID = _env("GRAPH_CLIENT_ID")
GRAPH_TENANT_ID = _env("GRAPH_TENANT_ID")

DATABASE_URL = _env("DATABASE_URL", "postgresql://assistente:assistente@db:5432/assistente")

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
LLM_MODEL = _env("LLM_MODEL", "claude-sonnet-5-5")
LLM_FAST_MODEL = _env("LLM_FAST_MODEL", "claude-haiku-5-5")

TZ = ZoneInfo(_env("TZ_NAME", "America/Sao_Paulo"))
POLL_INTERVAL = int(_env("POLL_INTERVAL_SECONDS", "300"))
BACKFILL_DAYS = int(_env("BACKFILL_DAYS", "7"))
DAILY_REPORT_TIME = _env("DAILY_REPORT_TIME", "18:00")

TOKEN_CACHE_PATH = _env("TOKEN_CACHE_PATH", "/data/msal_cache.bin")

# Chat de notas pessoais do Teams: é por ele que você conversa com o assistente
NOTES_CHAT_ID = "48:notes"
COMMAND_POLL_SECONDS = int(_env("COMMAND_POLL_SECONDS", "20"))
# Também envia lembretes e alertas de prioridade por e-mail (pede o escopo Mail.Send)
NOTIFY_EMAIL = _env("NOTIFY_EMAIL", "false").lower() in ("1", "true", "yes")

# Limite de texto enviado ao LLM por chat (mantém as mensagens mais recentes)
MAX_REPORT_CHARS = int(_env("MAX_REPORT_CHARS", "120000"))

# Alerta imediato para mensagens de pessoas com prioridade <= N (1 = só alta). Vazio desativa.
ALERT_PRIORITY = int(_env("ALERT_PRIORITY", "1") or 0)

# Aprendizado automático de memórias por pessoa: auto | review (vira sugestão) | off
LEARN_MODE = _env("LEARN_MODE", "auto")
LEARN_MIN_MESSAGES = int(_env("LEARN_MIN_MESSAGES", "10"))  # junta mensagens novas antes de chamar o LLM
LEARN_MAX_CHARS = int(_env("LEARN_MAX_CHARS", "40000"))     # tamanho máximo de cada lote enviado
LEARN_MAX_CALLS = int(_env("LEARN_MAX_CALLS", "6"))         # chamadas de aprendizado por ciclo do worker
AUTO_FACT_CAP = int(_env("AUTO_FACT_CAP", "40"))            # máx. de notas automáticas por pessoa

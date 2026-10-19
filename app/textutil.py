"""Funções puras (testáveis sem rede/banco)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

_TAG_BREAK = re.compile(r"<\s*(br|/p|/div|/li)\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_FRAC = re.compile(r"\.(\d+)")


def html_to_text(content: str) -> str:
    """Converte o HTML do corpo de uma mensagem do Teams em texto simples."""
    text = _TAG_BREAK.sub("\n", content or "")
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_dt(value: str | None) -> datetime | None:
    """Lê datas ISO da Graph (aceita 'Z' e frações com até 7+ dígitos)."""
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    value = _FRAC.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def graph_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_report_args(arg: str) -> tuple[int | None, int]:
    """'3 7d' -> (3, 7); '7d' -> (None, 7); '' -> (None, 1)."""
    chat_num: int | None = None
    days = 1
    for tok in arg.lower().split():
        if tok.endswith("d") and tok[:-1].isdigit():
            days = max(1, int(tok[:-1]))
        elif tok.isdigit():
            chat_num = int(tok)
    return chat_num, days


def split_name_text(arg: str) -> tuple[str, str]:
    """'Rafael Schmidt: cuida do S&OP' -> ('Rafael Schmidt', 'cuida do S&OP').
    Sem ':' usa a primeira palavra como nome."""
    if ":" in arg:
        name, _, text = arg.partition(":")
    else:
        name, _, text = arg.partition(" ")
    return name.strip(), text.strip()


def split_name_number(arg: str) -> tuple[str, int | None]:
    """'Rafael Schmidt 1' -> ('Rafael Schmidt', 1)."""
    name, _, last = arg.strip().rpartition(" ")
    if last.isdigit():
        return name.strip(), int(last)
    return arg.strip(), None


def parse_ids(arg: str) -> list[int] | None:
    """'12 13 #14' -> [12, 13, 14]; 'todas' -> None; inválido -> []."""
    if arg.strip().lower() in ("todas", "todos", "tudo", "all"):
        return None
    return [int(t.lstrip("#")) for t in arg.replace(",", " ").split() if t.lstrip("#").isdigit()]

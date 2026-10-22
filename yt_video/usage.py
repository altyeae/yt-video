from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

# USD per million tokens: (input, output incl. thinking). Check https://ai.google.dev/pricing and override in .env
# as GEMINI_PRICE_TEXT="0.30,2.50" and GEMINI_PRICE_IMAGE="0.30,30" if Google changes them.
DEFAULT_PRICES = {"text": (0.30, 2.50), "image": (0.30, 30.0)}
_lock = threading.Lock()
_totals: dict[str, dict[str, float]] = {}


def _prices(kind: str) -> tuple[float, float]:
    raw = os.environ.get(f"GEMINI_PRICE_{kind.upper()}", "")
    try:
        first, second = (float(part) for part in raw.split(","))
        return first, second
    except ValueError:
        return DEFAULT_PRICES[kind]


def reset() -> None:
    with _lock:
        _totals.clear()


def record(kind: str, payload: dict[str, Any]) -> None:
    """kind is 'text' or 'image'; payload is a Gemini generateContent response."""
    meta = payload.get("usageMetadata") or {}
    with _lock:
        row = _totals.setdefault(kind, {"calls": 0, "input": 0, "output": 0, "thinking": 0})
        row["calls"] += 1
        row["input"] += int(meta.get("promptTokenCount") or 0)
        row["output"] += int(meta.get("candidatesTokenCount") or 0)
        row["thinking"] += int(meta.get("thoughtsTokenCount") or 0)


def report(path: Path, voice_chars: int = 0) -> float:
    """cost.txt for one build; returns the estimate in roubles."""
    rate = float(os.environ.get("USD_RUB", "82"))
    lines = []
    total = 0.0
    with _lock:
        rows = {kind: dict(row) for kind, row in _totals.items()}
    for kind, row in sorted(rows.items()):
        price_in, price_out = _prices(kind)
        usd = row["input"] / 1e6 * price_in + (row["output"] + row["thinking"]) / 1e6 * price_out
        total += usd * rate
        lines.append(
            f"Gemini {kind}: {int(row['calls'])} запросов, вход {int(row['input']):,} токенов, ответ {int(row['output']):,}, "
            f"размышления {int(row['thinking']):,} — ~{usd * rate:.0f} ₽"
        )
    lines.append(f"Итого Gemini: ~{total:.0f} ₽ (курс {rate:g} ₽/$, цены в yt_video/usage.py)")
    if voice_chars:
        per_thousand = float(os.environ.get("ELEVENLABS_RUB_PER_1000", "25"))
        lines.append(f"ElevenLabs: {voice_chars:,} символов — ~{voice_chars / 1000 * per_thousand:.0f} ₽ по тарифу")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return total

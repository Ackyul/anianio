"""Registro y seguimiento automático de señales enviadas (TP vs SL) para /resumen."""
import json
import os
import time
from datetime import datetime, timezone

from .market_data import public_exchange

ROOT = os.path.dirname(os.path.dirname(__file__))
HISTORY_PATH = os.path.join(ROOT, "state", "signals_history.json")


def _load() -> list[dict]:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list[dict]):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, HISTORY_PATH)


def record_signals(picks: list[dict], source: str = "signals"):
    if not picks:
        return
    items = _load()
    now_ms = int(time.time() * 1000)
    open_keys = {(it["symbol"], it["side"]) for it in items if it["status"] == "OPEN"}
    for p in picks:
        if p.get("safety", 0) < 5.0:
            continue
        key = (p["symbol"], p["side"])
        if key in open_keys:
            continue
        items.append({
            "symbol": p["symbol"],
            "side": p["side"],
            "entry": float(p["entry"]),
            "tp": float(p["tp"]),
            "sl": float(p["sl"]),
            "tp_r": float(p.get("tp_r", 1.2)),
            "safety": float(p.get("safety", 0)),
            "source": source,
            "opened_ms": now_ms,
            "opened_iso": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "status": "OPEN",
            "closed_iso": None,
        })
        open_keys.add(key)
    _save(items[-200:])


def evaluate_history(items: list[dict] | None = None, ex=None) -> list[dict]:
    if items is None:
        items = _load()
    open_items = [it for it in items if it["status"] == "OPEN"]
    if not open_items:
        return items
    if ex is None:
        ex = public_exchange("futures")
    now_ms = int(time.time() * 1000)
    changed = False
    for it in open_items:
        if now_ms - it["opened_ms"] > 72 * 3600 * 1000:
            it["status"] = "EXPIRED"
            it["closed_iso"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            changed = True
            continue
        try:
            candles = ex.fetch_ohlcv(it["symbol"], "15m", since=it["opened_ms"], limit=300)
        except Exception:
            continue
        for ts, _o, h, l, _c, _v in candles:
            if ts < it["opened_ms"]:
                continue
            if it["side"] == "LONG":
                hit_sl = l <= it["sl"]
                hit_tp = h >= it["tp"]
            else:
                hit_sl = h >= it["sl"]
                hit_tp = l <= it["tp"]
            if hit_sl:
                it["status"] = "SL"
                it["closed_iso"] = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                changed = True
                break
            if hit_tp:
                it["status"] = "TP"
                it["closed_iso"] = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                changed = True
                break
    if changed:
        _save(items)
    return items


def format_summary() -> str:
    items = evaluate_history()
    if not items:
        return "Aun no hay señales registradas (>= 5.0/10). Cuando recibas señales nuevas se guardaran aqui."
    wins = [it for it in items if it["status"] == "TP"]
    losses = [it for it in items if it["status"] == "SL"]
    opened = [it for it in items if it["status"] == "OPEN"]
    closed = len(wins) + len(losses)
    winrate = (len(wins) / closed * 100) if closed else 0.0
    net_r = sum(it.get("tp_r", 1.2) for it in wins) - len(losses)

    lines = [
        "RESUMEN REAL DE SEÑALES (>= 5.0/10)",
        "-" * 42,
        f"Ganadas (TP): {len(wins)}  |  Perdidas (SL): {len(losses)}  |  Abiertas: {len(opened)}",
        f"Acierto:      {winrate:.1f}%   |  Resultado neto: {net_r:+.2f}R",
        "\nUltimas 5 señales:",
    ]
    for it in reversed(items[-5:]):
        pair = it["symbol"].split(":")[0].replace("/", "")
        lines.append(f"- {pair} {it['side']} ({it['opened_iso']}) -> {it['status']}")
    return "\n".join(lines)

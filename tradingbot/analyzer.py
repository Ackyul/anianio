"""Analizador multi-factor para trading manual (LONG / SHORT con Entrada, SL y TP)."""
from concurrent.futures import ThreadPoolExecutor
import time

import ccxt
import pandas as pd

from .config import Config
from .indicators import ema
from .market_data import fetch_recent, public_exchange, select_universe
from .strategy import build_panel, compute_features, compute_scores


def _funding_rates(ex: ccxt.Exchange, symbols: list[str]) -> dict[str, float]:
    try:
        raw = ex.fetch_funding_rates(symbols)
        return {k: float(v.get("fundingRate") or 0.0) for k, v in raw.items()}
    except Exception:
        return {}


def _trend_4h(ex: ccxt.Exchange, symbol: str) -> str:
    try:
        df4 = fetch_recent(ex, symbol, "4h", limit=250, include_live=True)
        if len(df4) < 200:
            return "NEUTRO"
        c = df4["close"]
        e20, e50, e200 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
        px = c.iloc[-1]
        if px > e50 and e20 > e50 > e200:
            return "ALCISTA"
        if px < e50 and e20 < e50 < e200:
            return "BAJISTA"
    except Exception:
        pass
    return "NEUTRO"


def _live_15m_ok(ex: ccxt.Exchange, symbol: str, side: str) -> bool:
    """Confirma en 15m en vivo que el impulso inmediato va a favor de la dirección."""
    try:
        df15 = fetch_recent(ex, symbol, "15m", limit=60, include_live=True)
        if len(df15) < 25:
            return False
        c = df15["close"]
        e20 = ema(c, 20).iloc[-1]
        e50 = ema(c, 50).iloc[-1]
        px = c.iloc[-1]
        return (px > e20 and px > e50) if side == "long" else (px < e20 and px < e50)
    except Exception:
        return False


def _trade_levels(df: pd.DataFrame, row: pd.Series, side: str, tp_r: float = 1.2, live_price: float | None = None) -> dict:
    entry = float(live_price) if live_price else float(row["close"])
    a = float(row["atr"])
    recent = df.iloc[-8:]
    if side == "long":
        swing_sl = float(recent["low"].min()) * 0.998
        sl = max(entry - 3.0 * a, min(swing_sl, entry - 2.0 * a))
        risk = max(entry - sl, a * 1.5)
        sl = entry - risk
        tp = entry + tp_r * risk
    else:
        swing_sl = float(recent["high"].max()) * 1.002
        sl = min(entry + 3.0 * a, max(swing_sl, entry + 2.0 * a))
        risk = max(sl - entry, a * 1.5)
        sl = entry + risk
        tp = entry - tp_r * risk
    return {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "tp_r": tp_r,
        "sl_pct": abs(entry - sl) / entry,
        "tp_pct": abs(tp - entry) / entry,
    }


_CACHE: dict[tuple, tuple[float, dict]] = {}


def analyze_market(cfg: Config, universe_limit: int = 80, top_picks: int = 5, safe_mode: bool = False) -> dict:
    cache_key = (universe_limit, top_picks, safe_mode)
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0] < 180):
        return cached[1]

    ex = public_exchange("futures")
    cfg_scan = Config(**{**cfg.__dict__, "market_type": "futures", "btc_symbol": "BTC/USDT:USDT",
                         "universe_size": 60 if safe_mode else universe_limit,
                         "min_quote_volume": 20e6 if safe_mode else 8e6})
    universe = select_universe(ex, cfg_scan)
    raw_dfs, feats = {}, {}

    def _load_one(sym: str):
        try:
            lim = 900 if sym == cfg_scan.btc_symbol else 300
            df = fetch_recent(ex, sym, "1h", lim, include_live=True)
            if len(df) > 220:
                return sym, df, compute_features(df, cfg_scan)
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        for res_item in pool.map(_load_one, universe):
            if res_item is not None:
                s, df, ft = res_item
                raw_dfs[s], feats[s] = df, ft

    panel = build_panel(feats)
    sc = compute_scores(panel, cfg_scan)
    funding = _funding_rates(ex, list(feats.keys()))
    try:
        tickers = ex.fetch_tickers(list(feats.keys()))
        live_prices = {s: float(t["last"]) for s, t in tickers.items() if t and t.get("last")}
    except Exception:
        live_prices = {}

    bull = bool(sc["bull_on"].iloc[-1])
    bear = bool(sc["bear_on"].iloc[-1])
    btc_ok = bool(sc["btc_ok"].iloc[-1])
    breadth = float(sc["breadth"].iloc[-1])
    score_row = sc["score"].iloc[-1]
    rank_l, rank_s = sc["rank"].iloc[-1], sc["rank_short"].iloc[-1]

    def build_picks(side: str) -> list[dict]:
        rank_row = rank_l if side == "long" else rank_s
        candidates = []
        for sym in rank_row.dropna().sort_values().index[:80]:
            f = feats[sym].iloc[-1]
            s = float(score_row[sym])
            fr = funding.get(sym, 0.0)
            rsi_val = float(f["rsi"])
            if side == "long":
                if s <= 0 or f["trend_long"] <= 0 or not (35 <= rsi_val <= 68) or fr > 0.0005:
                    continue
            else:
                if s >= 0 or f["trend_short"] <= 0 or not (33 <= rsi_val <= 65) or fr < -0.0005:
                    continue
            has_trigger = bool(f["long_sig" if side == "long" else "short_sig"] > 0)
            if safe_mode and (not has_trigger or abs(float(f["vol_surge"])) < 0.8):
                continue
            lvl = _trade_levels(raw_dfs[sym], f, side, tp_r=1.0 if safe_mode else 1.2,
                                live_price=live_prices.get(sym))
            max_sl = 0.04 if safe_mode else 0.055
            if lvl["sl_pct"] > max_sl:
                continue
            candidates.append((sym, f, s, fr, rsi_val, has_trigger, lvl))
            if len(candidates) >= top_picks * 3:
                break

        def _check_mtf(item):
            sym, f, s, fr, rsi_val, has_trigger, lvl = item
            t4h = _trend_4h(ex, sym)
            if (side == "long" and t4h != "ALCISTA") or (side == "short" and t4h != "BAJISTA"):
                return None
            if not _live_15m_ok(ex, sym, side):
                return None
            return sym, f, s, fr, rsi_val, has_trigger, lvl, t4h

        picks = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            checked = list(pool.map(_check_mtf, candidates))

        for item in checked:
            if item is None:
                continue
            sym, f, s, fr, rsi_val, has_trigger, lvl, t4h = item
            reasons = [
                f"Score {s:+.2f}",
                f"4h+1h+15m {t4h}",
                f"RSI {rsi_val:.0f}",
                f"Funding {fr * 100:+.3f}%",
            ]
            if has_trigger:
                reasons.append("Gatillo 1h activo")
            with_market = (side == "long" and bull) or (side == "short" and bear)
            vol_ok = abs(float(f["vol_surge"])) >= 1.0
            safety = (
                (3.0 if with_market else 0.0)
                + (2.0 if has_trigger else 0.0)
                + min(abs(s), 2.5)
                + max(0.0, (0.055 - lvl["sl_pct"]) * 30.0)
                + (1.0 if 40 <= rsi_val <= 60 else 0.5)
                + (0.5 if vol_ok else 0.0)
            )
            safety_score = round(min(10.0, safety), 1)
            if with_market:
                reasons.insert(0, "A favor del mercado")
            risk_usdt = cfg.account_balance * cfg.manual_risk_pct
            notional = min(risk_usdt / max(lvl["sl_pct"], 0.005), cfg.account_balance * 2.0)
            margin_2x = notional / 2.0
            picks.append({
                "symbol": sym,
                "side": side.upper(),
                "score": s,
                "safety": safety_score,
                "t4h": t4h,
                "trigger": has_trigger,
                "ema20": float(f["ema20"]),
                "funding": fr,
                "rsi": rsi_val,
                "margin_2x": margin_2x,
                "notional": notional,
                "risk_usdt": risk_usdt,
                "reasons": reasons,
                **lvl,
            })
            if len(picks) >= top_picks:
                break
        return picks

    if safe_mode:
        longs = build_picks("long") if bull else []
        shorts = build_picks("short") if bear else []
    else:
        longs = build_picks("long")
        shorts = build_picks("short")
    out = {
        "universe_count": len(feats),
        "btc_ok": btc_ok,
        "breadth": breadth,
        "safe_mode": safe_mode,
        "regime": "ALCISTA (priorizar LONG)" if bull else ("BAJISTA (priorizar SHORT)" if bear else "MIXTO (precaución / tamaño reducido)"),
        "longs": longs,
        "shorts": shorts,
    }
    _CACHE[cache_key] = (time.time(), out)
    return out


def format_analysis(res: dict) -> str:
    mode_tag = " [NOCTURNO SEGURO]" if res.get("safe_mode") else ""
    lines = [
        f"ANALISIS BINANCE FUTURES ({res['universe_count']} monedas){mode_tag}",
        f"Mercado: {res['regime']}",
        f"BTC > EMA800h: {'SI' if res['btc_ok'] else 'NO'} | Amplitud: {res['breadth']:.0%}",
        "-" * 50,
    ]
    items = sorted(res["longs"] + res["shorts"], key=lambda p: p.get("safety", 0), reverse=True)
    if not items:
        lines.append("\nNO OPERAR AHORA.")
        lines.append("Ninguna moneda cumple todos los filtros (4h + Gatillo 1h + SL <= 4.5% + Funding sano).")
        lines.append("Protege tu capital en USDT y vuelve a consultar al cierre de la proxima hora.")
        return "\n".join(lines)

    if items[0]["safety"] < 5.0:
        lines.append("\nAVISO: Señales de baja seguridad (<5/10, contra-mercado). Mejor esperar o usar tamaño minimo.")

    for i, p in enumerate(items, 1):
        pair = p["symbol"].split(":")[0].replace("/", "")
        lines.append(f"\n{i}. {pair}  ->  {p['side']}  (Seguridad: {p['safety']:.1f}/10)")
        lines.append(f"   Entrada:     {p['entry']:.6g} (precio actual)")
        lines.append(f"   Take Profit: {p['tp']:.6g}  (+{p['tp_pct']:.2%})")
        lines.append(f"   Stop Loss:   {p['sl']:.6g}  (-{p['sl_pct']:.2%})")
        lines.append(f"   Tamaño (2x): Pon {p['margin_2x']:.1f} de margen (orden {p['notional']:.1f} | riesgo -{p['risk_usdt']:.2f})")
        lines.append(f"   Motivo:      {', '.join(p['reasons'])}")
    return "\n".join(lines)

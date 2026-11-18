"""
Estrategia: ROTACIÓN POR MOMENTUM LONG / SHORT con filtro de régimen de mercado.

1. Régimen de mercado:
   - ALCISTA (habilita LONG): BTC > EMA800h y >= 40% del universo > EMA200h.
   - BAJISTA (habilita SHORT, si allow_short=True): BTC < EMA800h y <= 40% del universo > EMA200h.
   - MIXTO: no abre nuevas operaciones (protege del mercado lateral).
2. Ranking: cada hora se puntúa cada moneda del universo combinando (z-scores):
     - 40% momentum ajustado por volatilidad (retornos 1d/3d/7d / volatilidad)
     - 25% fuerza relativa contra BTC (7 días)
     - 20% calidad de tendencia (pendiente * R² de regresión sobre 72h)
     - 15% aumento de volumen (24h vs 7d; con signo según dirección de la tendencia)
3. Entradas:
   - LONG: top N del ranking (score > 0), EMA20 > EMA50 > EMA200, RSI < 75, no sobreextendida,
     ruptura de máximo de 20 velas o rebote alcista en EMA20.
   - SHORT: bottom N del ranking (score < 0), EMA20 < EMA50 < EMA200, RSI > 25, no sobreextendida,
     ruptura de mínimo de 20 velas o rechazo bajista en EMA20.
4. Salidas:
   - Stop inicial 2.5 ATR, trailing stop 3 ATR desde el máximo (long) o mínimo (short),
     break-even al ganar 1R, y salida si la tendencia se rompe (cierre cruza EMA50).
"""
import numpy as np
import pandas as pd

from .config import Config
from .indicators import atr, ema, rsi, trend_quality


def compute_features(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    c = df["close"]
    out = pd.DataFrame(index=df.index)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = df[col].astype(float)

    out["ema20"] = ema(c, 20)
    out["ema50"] = ema(c, 50)
    out["ema200"] = ema(c, 200)
    out["ema_regime"] = ema(c, cfg.regime_ema)
    out["atr"] = atr(df, 14)
    out["rsi"] = rsi(c, 14)

    out["ret_24"] = c.pct_change(24)
    out["ret_72"] = c.pct_change(72)
    out["ret_168"] = c.pct_change(168)
    vol = np.log(c).diff().rolling(168).std() * np.sqrt(168)
    mom = 0.2 * out["ret_24"] + 0.3 * out["ret_72"] + 0.5 * out["ret_168"]
    out["mom_adj"] = mom / vol.replace(0, np.nan)

    slope, r2 = trend_quality(c, 72)
    out["trend_q"] = slope * 24 * r2

    qv = df["volume"] * c
    vol_ratio = (qv.rolling(24).mean() / qv.rolling(168).mean().replace(0, np.nan)).clip(0, 3)
    out["vol_surge"] = vol_ratio * np.sign(out["ret_72"]).fillna(0)

    # --- Señales LONG ---
    don_high = df["high"].rolling(20).max().shift(1)
    touched_low = (df["low"] <= out["ema20"] * 1.003).astype(float).rolling(4).max()
    trend_long = (c > out["ema50"]) & (out["ema50"] > out["ema200"]) & (out["ema20"] > out["ema50"])
    ext_long = (c - out["ema20"]) / out["atr"]
    not_ext_long = (out["rsi"] < cfg.rsi_max) & (ext_long < cfg.max_ext_atr)
    breakout_long = c > don_high
    pullback_long = (touched_low == 1) & (c > out["ema20"]) & (c > df["open"]) & out["rsi"].between(45, 68)

    out["trend_long"] = trend_long.astype(float)
    out["long_sig"] = (trend_long & not_ext_long & (breakout_long | pullback_long)).astype(float)

    # --- Señales SHORT ---
    don_low = df["low"].rolling(20).min().shift(1)
    touched_high = (df["high"] >= out["ema20"] * 0.997).astype(float).rolling(4).max()
    trend_short = (c < out["ema50"]) & (out["ema50"] < out["ema200"]) & (out["ema20"] < out["ema50"])
    ext_short = (out["ema20"] - c) / out["atr"]
    not_ext_short = (out["rsi"] > cfg.rsi_min) & (ext_short < cfg.max_ext_atr)
    breakout_short = c < don_low
    pullback_short = (touched_high == 1) & (c < out["ema20"]) & (c < df["open"]) & out["rsi"].between(32, 55)

    out["trend_short"] = trend_short.astype(float)
    out["short_sig"] = (trend_short & not_ext_short & (breakout_short | pullback_short)).astype(float)

    # Alias por compatibilidad
    out["trend_ok"] = out["trend_long"]
    out["entry_sig"] = out["long_sig"]
    return out


def build_panel(feats: dict) -> dict:
    """dict símbolo->DataFrame  =>  dict columna->DataFrame (tiempo x símbolo)."""
    cols = next(iter(feats.values())).columns
    return {col: pd.concat({s: f[col] for s, f in feats.items()}, axis=1).sort_index() for col in cols}


def compute_scores(panel: dict, cfg: Config) -> dict:
    btc = cfg.btc_symbol
    ret = panel["ret_168"]
    rs = ret.sub(ret[btc], axis=0)
    comps = {
        "mom": panel["mom_adj"],
        "rs": rs,
        "trend": panel["trend_q"],
        "vs": panel["vol_surge"],
    }
    valid = panel["ema200"].notna()
    for v in comps.values():
        valid &= v.notna()

    def z(x):
        x = x.where(valid)
        sd = x.std(axis=1, ddof=0).replace(0, np.nan)
        return x.sub(x.mean(axis=1), axis=0).div(sd, axis=0).fillna(0).where(valid)

    score = 0.40 * z(comps["mom"]) + 0.25 * z(comps["rs"]) + 0.20 * z(comps["trend"]) + 0.15 * z(comps["vs"])
    rank_long = score.rank(axis=1, ascending=False, method="first")
    rank_short = score.rank(axis=1, ascending=True, method="first")
    breadth = (panel["close"] > panel["ema200"]).astype(float).where(valid).mean(axis=1)
    btc_ok = panel["close"][btc] > panel["ema_regime"][btc]
    bull_on = btc_ok & (breadth >= cfg.min_breadth)
    bear_on = (~btc_ok) & (breadth <= cfg.min_breadth) if cfg.allow_short else pd.Series(False, index=bull_on.index)
    return {
        "score": score,
        "rank": rank_long,
        "rank_short": rank_short,
        "breadth": breadth,
        "btc_ok": btc_ok,
        "regime_on": bull_on,
        "bull_on": bull_on,
        "bear_on": bear_on,
    }


def select_entries(score_row: pd.Series, rank_row: pd.Series, sig_row: pd.Series,
                   held: set, slots: int, cfg: Config, side: str = "long") -> list:
    picks = []
    if slots <= 0:
        return picks
    for sym in rank_row.dropna().sort_values().index:
        if rank_row[sym] > cfg.top_n_rank:
            break
        sc = score_row[sym]
        if sym in held or sig_row.get(sym, 0) <= 0:
            continue
        if side == "long" and not (sc > 0):
            continue
        if side == "short" and not (sc < 0):
            continue
        picks.append(sym)
        if len(picks) >= slots:
            break
    return picks


def initial_stop(entry_price: float, atr_value: float, cfg: Config, side: str = "long") -> float:
    if side == "short":
        return entry_price + cfg.atr_stop_mult * atr_value
    return entry_price - cfg.atr_stop_mult * atr_value


def update_stop(pos: dict, high: float, close: float, atr_value: float, cfg: Config, low: float | None = None) -> float:
    """Trailing stop tipo chandelier + break-even al alcanzar 1R (soporta long y short)."""
    side = pos.get("side", "long")
    if side == "short":
        lo = low if low is not None else close
        pos["lowest"] = min(pos.get("lowest", pos["entry"]), lo)
        new = min(pos["stop"], pos["lowest"] + cfg.atr_trail_mult * atr_value)
        r = pos["initial_stop"] - pos["entry"]
        if r > 0 and close <= pos["entry"] - r:
            new = min(new, pos["entry"] * (1 - 2 * cfg.fee_rate))
        pos["stop"] = new
        return new

    pos["highest"] = max(pos.get("highest", pos["entry"]), high)
    new = max(pos["stop"], pos["highest"] - cfg.atr_trail_mult * atr_value)
    r = pos["entry"] - pos["initial_stop"]
    if r > 0 and close >= pos["entry"] + r:
        new = max(new, pos["entry"] * (1 + 2 * cfg.fee_rate))
    pos["stop"] = new
    return new


def trend_broken(close: float, ema50: float, side: str = "long") -> bool:
    if side == "short":
        return close > ema50
    return close < ema50

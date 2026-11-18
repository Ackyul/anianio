"""Backtest vela a vela con EXACTAMENTE la misma lógica que el bot en vivo (soporta LONG y SHORT).
Decisiones al cierre de la vela t, ejecución a la apertura de la vela t+1. Incluye comisiones y slippage."""
import os

import numpy as np
import pandas as pd

from .config import Config
from .strategy import build_panel, compute_features, compute_scores, initial_stop, trend_broken, update_stop

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "results")


def run_backtest(data: dict, cfg: Config, capital: float = 1000.0) -> dict:
    feats = {s: compute_features(df, cfg) for s, df in data.items() if len(df) > 300}
    panel = build_panel(feats)
    sc = compute_scores(panel, cfg)
    idx = panel["close"].index
    syms = list(panel["close"].columns)

    O, H, L, C = (panel[k].values for k in ("open", "high", "low", "close"))
    ATR, E50 = panel["atr"].values, panel["ema50"].values
    SIG_L, SIG_S = panel["long_sig"].values, panel["short_sig"].values
    CF = panel["close"].ffill().values
    score = sc["score"].values
    rank_l, rank_s = sc["rank"].values, sc["rank_short"].values
    bull_on, bear_on = sc["bull_on"].values, sc["bear_on"].values
    start = int(np.argmax(panel["ema_regime"][cfg.btc_symbol].notna().values)) + 1

    cash, peak = capital, capital
    positions, trades, curve = {}, [], []
    pending_exit, pending_entry = {}, []  # pending_entry: list of (j, side)
    day, day_start, day_halt, halted = None, capital, False, False
    equity = capital

    def close_pos(j, px_raw, reason, i):
        nonlocal cash
        p = positions.pop(j)
        side = p["side"]
        px = px_raw * (1 - cfg.slippage) if side == "long" else px_raw * (1 + cfg.slippage)
        exit_notional = p["qty"] * px
        exit_fee = exit_notional * cfg.fee_rate
        if side == "long":
            gross_pnl = p["qty"] * (px - p["entry"])
        else:
            gross_pnl = p["qty"] * (p["entry"] - px)
        pnl = gross_pnl - p["entry_fee"] - exit_fee
        # Liberar margen bloqueado + PnL neto
        cash += p["margin"] + gross_pnl - exit_fee
        risk = p["qty"] * abs(p["entry"] - p["initial_stop"])
        trades.append({
            "symbol": syms[j], "side": side, "entry_time": idx[p["i"]], "exit_time": idx[i],
            "entry": p["entry"], "exit": px, "pnl": pnl, "ret": pnl / p["margin"],
            "R": pnl / risk if risk > 0 else 0, "bars": i - p["i"], "reason": reason,
        })

    for i in range(start, len(idx)):
        # a) ejecutar órdenes pendientes a la apertura
        for j, reason in list(pending_exit.items()):
            if j in positions and not np.isnan(O[i, j]):
                close_pos(j, O[i, j], reason, i)
                pending_exit.pop(j)
        for j, side in pending_entry:
            o, a = O[i, j], ATR[i - 1, j]
            if np.isnan(o) or np.isnan(a) or j in positions or len(positions) >= cfg.max_positions:
                continue
            px = o * (1 + cfg.slippage) if side == "long" else o * (1 - cfg.slippage)
            stop = initial_stop(px, a, cfg, side=side)
            if stop <= 0:
                continue
            stop_dist = abs(px - stop)
            if stop_dist <= 0:
                continue
            qty = equity * cfg.risk_per_trade / stop_dist
            notional = min(qty * px, equity * cfg.max_position_pct, cash / (1 + cfg.fee_rate))
            if notional < cfg.min_notional:
                continue
            entry_fee = notional * cfg.fee_rate
            cash -= (notional + entry_fee)
            positions[j] = {
                "side": side, "qty": notional / px, "entry": px, "stop": stop,
                "initial_stop": stop, "highest": px, "lowest": px,
                "margin": notional, "entry_fee": entry_fee, "i": i,
            }
        pending_entry = []

        # b) stops dentro de la vela
        for j in list(positions):
            p = positions[j]
            if np.isnan(L[i, j]) or np.isnan(H[i, j]):
                continue
            if p["side"] == "long":
                if O[i, j] <= p["stop"]:
                    close_pos(j, O[i, j], "stop(gap)", i)
                elif L[i, j] <= p["stop"]:
                    close_pos(j, p["stop"], "stop", i)
            else:
                if O[i, j] >= p["stop"]:
                    close_pos(j, O[i, j], "stop(gap)", i)
                elif H[i, j] >= p["stop"]:
                    close_pos(j, p["stop"], "stop", i)

        # c) cierre de vela: valoración, riesgo, decisiones
        unrealized = 0.0
        locked = 0.0
        for j, p in positions.items():
            locked += p["margin"]
            cur = CF[i, j]
            if p["side"] == "long":
                unrealized += p["qty"] * (cur - p["entry"])
            else:
                unrealized += p["qty"] * (p["entry"] - cur)
        equity = cash + locked + unrealized
        peak = max(peak, equity)
        d = idx[i].date()
        if d != day:
            day, day_start, day_halt = d, equity, False
        if equity <= day_start * (1 - cfg.max_daily_loss):
            day_halt = True
        if equity <= peak * (1 - cfg.max_drawdown):
            halted = True
        curve.append((idx[i], equity))

        for j, p in positions.items():
            if np.isnan(C[i, j]):
                continue
            update_stop(p, H[i, j], C[i, j], ATR[i, j], cfg, low=L[i, j])
            if trend_broken(C[i, j], E50[i, j], side=p["side"]):
                pending_exit[j] = "tendencia rota"

        if not halted and not day_halt:
            slots = cfg.max_positions - len(positions)
            if bull_on[i] and slots > 0:
                for j in np.argsort(rank_l[i]):
                    if slots <= 0 or np.isnan(rank_l[i, j]) or rank_l[i, j] > cfg.top_n_rank:
                        break
                    if j in positions or not (score[i, j] > 0) or not (SIG_L[i, j] > 0):
                        continue
                    pending_entry.append((j, "long"))
                    slots -= 1
            elif bear_on[i] and slots > 0:
                for j in np.argsort(rank_s[i]):
                    if slots <= 0 or np.isnan(rank_s[i, j]) or rank_s[i, j] > cfg.top_n_rank:
                        break
                    if j in positions or not (score[i, j] < 0) or not (SIG_S[i, j] > 0):
                        continue
                    pending_entry.append((j, "short"))
                    slots -= 1

    eq = pd.Series(dict(curve))
    btc = panel["close"][cfg.btc_symbol].loc[eq.index]
    return {"equity": eq, "trades": pd.DataFrame(trades), "btc": btc / btc.iloc[0] * capital,
            "halted": halted, "capital": capital}


def report(res: dict) -> str:
    eq, tr, cap = res["equity"], res["trades"], res["capital"]
    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / cap - 1
    cagr = (eq.iloc[-1] / cap) ** (1 / years) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = rets.mean() / rets.std() * np.sqrt(24 * 365) if rets.std() > 0 else 0
    btc_total = res["btc"].iloc[-1] / cap - 1
    btc_dd = (res["btc"] / res["btc"].cummax() - 1).min()
    lines = [
        "=" * 60,
        f"Periodo:            {eq.index[0]:%Y-%m-%d} -> {eq.index[-1]:%Y-%m-%d}",
        f"Capital inicial:    {cap:,.2f} USDT   ->  final: {eq.iloc[-1]:,.2f} USDT",
        f"Retorno total:      {total:+.2%}    (BTC buy&hold: {btc_total:+.2%})",
        f"Retorno anualizado: {cagr:+.2%}",
        f"Máx. drawdown:      {dd:.2%}     (BTC: {btc_dd:.2%})",
        f"Sharpe:             {sharpe:.2f}",
    ]
    if len(tr):
        wins = tr[tr.pnl > 0]
        losses = tr[tr.pnl <= 0]
        pf = wins.pnl.sum() / -losses.pnl.sum() if len(losses) and losses.pnl.sum() < 0 else float("inf")
        n_long = int((tr.side == "long").sum()) if "side" in tr else len(tr)
        n_short = int((tr.side == "short").sum()) if "side" in tr else 0
        lines += [
            f"Operaciones:        {len(tr)} (Long: {n_long} / Short: {n_short})  ganadoras: {len(wins) / len(tr):.1%}",
            f"Profit factor:      {pf:.2f}",
            f"R promedio:         {tr.R.mean():+.2f}   duración media: {tr.bars.mean():.0f}h",
            f"Mejor / peor:       {tr.ret.max():+.2%} / {tr.ret.min():+.2%}",
        ]
    else:
        lines.append("Operaciones:        0")
    if res["halted"]:
        lines.append("!! Se alcanzó el drawdown máximo: el bot se habría detenido.")
    lines.append("=" * 60)
    return "\n".join(lines)


def save_results(res: dict) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    res["equity"].rename("equity").to_csv(os.path.join(RESULTS_DIR, "equity.csv"))
    res["trades"].to_csv(os.path.join(RESULTS_DIR, "trades.csv"), index=False)
    return RESULTS_DIR

"""Bucle principal del bot en vivo (papel / testnet / real), soporta LONG y SHORT."""
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone

import ccxt

from .broker import BinanceBroker, PaperBroker
from .config import Config
from .market_data import fetch_recent, public_exchange, select_universe
from .strategy import build_panel, compute_features, compute_scores, initial_stop, select_entries, trend_broken, update_stop

ROOT = os.path.dirname(os.path.dirname(__file__))
STATE_DIR = os.path.join(ROOT, "state")
LOG_DIR = os.path.join(ROOT, "logs")
STOP_FILE = os.path.join(ROOT, "STOP")    # crea este archivo => no abre nuevas operaciones
PANIC_FILE = os.path.join(ROOT, "PANIC")  # crea este archivo => cierra todo lo del bot y se apaga
log = logging.getLogger("bot")


class Bot:
    def __init__(self, cfg: Config, mode: str):
        self.cfg, self.mode = cfg, mode
        self.data_ex = public_exchange(cfg.market_type)
        if mode == "paper":
            self.broker = PaperBroker(cfg, self.data_ex)
        else:
            self.broker = BinanceBroker(cfg, testnet=(mode == "testnet"))
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        self.state_path = os.path.join(STATE_DIR, f"{mode}_{cfg.market_type}.json")
        self.trades_path = os.path.join(LOG_DIR, f"trades_{mode}_{cfg.market_type}.csv")
        self.universe, self.universe_ts = [], 0.0
        self.state = self._load_state()

    # ---------------- estado ----------------
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        if self.broker.is_paper:
            cash = self.cfg.paper_capital
        else:
            cash = min(self.broker.usdt_free(), self.cfg.max_capital)
        log.info(f"Estado nuevo ({self.cfg.market_type}). Capital asignado al bot: {cash:.2f} USDT")
        return {"cash": cash, "start_capital": cash, "positions": {}, "peak": cash, "day": None,
                "day_start": cash, "day_halt": False, "halted": False, "last_cycle_hour": None}

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, self.state_path)

    def _log_trade(self, sym, p, exit_px, pnl, reason):
        new = not os.path.exists(self.trades_path)
        with open(self.trades_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["exit_time", "symbol", "side", "entry", "exit", "qty", "margin", "pnl", "ret", "reason"])
            w.writerow([datetime.now(timezone.utc).isoformat(), sym, p["side"], p["entry"], exit_px, p["qty"],
                        round(p["margin"], 4), round(pnl, 4), f"{pnl / p['margin']:.4f}", reason])
        log.info(f"CIERRE {p['side'].upper()} {sym} @ {exit_px:.6g}  PnL {pnl:+.2f} USDT ({pnl / p['margin']:+.2%})  motivo: {reason}")

    # ---------------- operaciones ----------------
    def _settle_close(self, sym: str, p: dict, exit_px: float, exit_fee: float, reason: str):
        if p["side"] == "long":
            gross = p["qty"] * (exit_px - p["entry"])
        else:
            gross = p["qty"] * (p["entry"] - exit_px)
        pnl = gross - p.get("entry_fee", 0.0) - exit_fee
        self.state["cash"] += p["margin"] + gross - exit_fee
        self._log_trade(sym, p, exit_px, pnl, reason)
        del self.state["positions"][sym]
        self.save()

    def close(self, sym: str, reason: str):
        p = self.state["positions"][sym]
        self.broker.cancel(sym, p.get("stop_id"))
        exit_px, exit_fee = self.broker.close_order(sym, p["side"], p["qty"])
        self._settle_close(sym, p, exit_px, exit_fee, reason)

    def open(self, sym: str, side: str, price: float, atr_value: float, equity: float):
        cfg = self.cfg
        stop = initial_stop(price, atr_value, cfg, side=side)
        if stop <= 0:
            return
        stop_dist = abs(price - stop)
        if stop_dist <= 0:
            return
        qty_risk = equity * cfg.risk_per_trade / stop_dist
        notional = min(qty_risk * price, equity * cfg.max_position_pct,
                       self.state["cash"] * 0.99, self.broker.usdt_free() * 0.99)
        if notional < cfg.min_notional * 1.1:
            log.info(f"{sym}: tamaño {notional:.2f} USDT demasiado pequeño, se omite")
            return
        qty, margin, entry_fee, avg = self.broker.open_order(sym, side, notional)
        stop = initial_stop(avg, atr_value, cfg, side=side)
        self.state["cash"] -= (margin + entry_fee)
        pos = {"side": side, "qty": qty, "entry": avg, "stop": stop, "initial_stop": stop,
               "highest": avg, "lowest": avg, "margin": margin, "entry_fee": entry_fee,
               "opened": datetime.now(timezone.utc).isoformat(), "stop_id": None}
        self.state["positions"][sym] = pos
        self.save()
        log.info(f"APERTURA {side.upper()} {sym} {qty:.6g} @ {avg:.6g} ({margin:.2f} USDT)  stop {stop:.6g}")
        try:
            pos["stop_id"] = self.broker.place_stop(sym, side, qty, stop)
            self.save()
        except Exception as e:
            log.error(f"No se pudo colocar el stop de {sym} ({e}). Por seguridad se cierra.")
            self.close(sym, "fallo al colocar stop")

    def move_stop(self, sym: str, old_stop: float):
        p = self.state["positions"][sym]
        if self.broker.is_paper:
            return
        self.broker.cancel(sym, p.get("stop_id"))
        try:
            p["stop_id"] = self.broker.place_stop(sym, p["side"], p["qty"], p["stop"])
        except Exception as e:
            log.error(f"Fallo al mover stop de {sym}: {e}. Se cierra la posición.")
            self.close(sym, "fallo al mover stop")
            return
        log.info(f"Stop {p['side'].upper()} {sym} movido {old_stop:.6g} -> {p['stop']:.6g}")

    def check_stops(self):
        for sym in list(self.state["positions"]):
            p = self.state["positions"][sym]
            side = p["side"]
            price = self.broker.price(sym)
            hit = (price <= p["stop"]) if side == "long" else (price >= p["stop"])
            if self.broker.is_paper:
                if hit:
                    self.close(sym, "stop")
                continue
            o = self.broker.stop_status(sym, p.get("stop_id"))
            status = (o or {}).get("status")
            if status == "closed":
                px = float(o.get("average") or o.get("price"))
                fee = BinanceBroker._fees_usdt(o)
                self._settle_close(sym, p, px, fee, "stop (exchange)")
            elif status != "open":
                log.warning(f"Stop de {sym} no está activo ({status}). Recolocando.")
                if hit:
                    self.close(sym, "stop (recolocación)")
                else:
                    self.move_stop(sym, p["stop"])
            elif hit and self.cfg.market_type == "spot":
                self.close(sym, "stop (gap, venta a mercado)")

    def equity(self, prices: dict) -> float:
        total = self.state["cash"]
        for s, p in self.state["positions"].items():
            cur = prices.get(s, p["entry"])
            unr = p["qty"] * (cur - p["entry"]) if p["side"] == "long" else p["qty"] * (p["entry"] - cur)
            total += p["margin"] + unr
        return total

    # ---------------- ciclo horario ----------------
    def cycle(self):
        cfg, st = self.cfg, self.state
        if time.time() - self.universe_ts > 6 * 3600 or not self.universe:
            self.universe = select_universe(self.data_ex, cfg, self.broker.tradable_symbols())
            self.universe_ts = time.time()
            log.info(f"Universo actualizado: {len(self.universe)} monedas")
        symbols = list(dict.fromkeys(self.universe + list(st["positions"])))
        feats = {}
        for s in symbols:
            try:
                df = fetch_recent(self.data_ex, s, cfg.timeframe, 1000)
                if len(df) > 300:
                    feats[s] = compute_features(df, cfg)
            except Exception as e:
                log.warning(f"Sin datos para {s}: {e}")
        panel = build_panel(feats)
        sc = compute_scores(panel, cfg)
        last = {k: v.iloc[-1] for k, v in panel.items()}
        prices = last["close"].dropna().to_dict()

        # gestionar posiciones abiertas
        for sym in list(st["positions"]):
            p = st["positions"][sym]
            if sym not in feats:
                continue
            row = feats[sym].iloc[-1]
            if trend_broken(row["close"], row["ema50"], side=p["side"]):
                self.close(sym, "tendencia rota")
                continue
            old = p["stop"]
            new = update_stop(p, row["high"], row["close"], row["atr"], cfg, low=row["low"])
            moved = (new > old * 1.001) if p["side"] == "long" else (new < old * 0.999)
            if moved:
                self.move_stop(sym, old)
        self.save()

        # control de riesgo
        eq = self.equity(prices)
        st["peak"] = max(st["peak"], eq)
        today = datetime.now(timezone.utc).date().isoformat()
        if st["day"] != today:
            st["day"], st["day_start"], st["day_halt"] = today, eq, False
        if eq <= st["day_start"] * (1 - cfg.max_daily_loss) and not st["day_halt"]:
            st["day_halt"] = True
            log.warning("Pérdida diaria máxima alcanzada: no se abren más operaciones hoy.")
        if eq <= st["peak"] * (1 - cfg.max_drawdown) and not st["halted"]:
            st["halted"] = True
            log.error("DRAWDOWN MÁXIMO alcanzado: el bot deja de abrir operaciones.")

        bull = bool(sc["bull_on"].iloc[-1])
        bear = bool(sc["bear_on"].iloc[-1])
        breadth = float(sc["breadth"].iloc[-1])
        score_row = sc["score"].iloc[-1]
        rank_l, rank_s = sc["rank"].iloc[-1], sc["rank_short"].iloc[-1]
        reg_txt = "ALCISTA (LONG)" if bull else ("BAJISTA (SHORT)" if bear else "MIXTO (ESPERA)")
        pos_txt = [f"{s.split('/')[0]}:{p['side']}" for s, p in st["positions"].items()]
        log.info(f"Equity {eq:.2f} USDT | caja {st['cash']:.2f} | pos {pos_txt} | régimen {reg_txt} (amplitud {breadth:.0%})")

        blocked = st["halted"] or st["day_halt"] or os.path.exists(STOP_FILE)
        if not blocked and (bull or bear):
            slots = cfg.max_positions - len(st["positions"])
            side = "long" if bull else "short"
            rank_row = rank_l if bull else rank_s
            sig_col = "long_sig" if bull else "short_sig"
            picks = select_entries(score_row, rank_row, last[sig_col], set(st["positions"]), slots, cfg, side=side)
            for sym in picks:
                try:
                    self.open(sym, side, prices[sym], float(last["atr"][sym]), eq)
                except Exception as e:
                    log.error(f"Error abriendo {side} en {sym}: {e}")
        self.save()

    def panic(self):
        log.warning("PANIC: cerrando todas las posiciones del bot.")
        for sym in list(self.state["positions"]):
            try:
                self.close(sym, "PANIC")
            except Exception as e:
                log.error(f"No se pudo cerrar {sym}: {e}")

    def run(self):
        log.info(f"Bot iniciado en modo {self.mode.upper()} ({self.cfg.market_type.upper()}, long+short={'SÍ' if self.cfg.allow_short else 'NO'}).")
        while True:
            try:
                if os.path.exists(PANIC_FILE):
                    self.panic()
                    return
                self.check_stops()
                now = time.time()
                hour = int(now // 3600)
                if self.state.get("last_cycle_hour") != hour and now % 3600 >= 20:
                    self.cycle()
                    self.state["last_cycle_hour"] = hour
                    self.save()
            except ccxt.NetworkError as e:
                log.warning(f"Error de red (se reintenta): {e}")
            except Exception:
                log.exception("Error inesperado")
            time.sleep(30)

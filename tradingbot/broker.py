"""Ejecución de órdenes (Spot y Futures Long/Short). PaperBroker = simulado. BinanceBroker = testnet o real."""
import logging

import ccxt

from .config import Config

log = logging.getLogger("bot")


class PaperBroker:
    is_paper = True

    def __init__(self, cfg: Config, data_ex: ccxt.Exchange):
        self.cfg, self.ex = cfg, data_ex

    def tradable_symbols(self):
        return None

    def price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])

    def usdt_free(self) -> float:
        return float("inf")

    def open_order(self, symbol: str, side: str, usdt: float):
        """Abre long ('long') o short ('short'). Devuelve (qty, margin_used, entry_fee, avg_px)."""
        raw_px = self.price(symbol)
        px = raw_px * (1 + self.cfg.slippage) if side == "long" else raw_px * (1 - self.cfg.slippage)
        qty = usdt / px
        fee = usdt * self.cfg.fee_rate
        return qty, usdt, fee, px

    def close_order(self, symbol: str, side: str, qty: float):
        """Cierra la posición opuesta. Devuelve (avg_px, exit_fee)."""
        raw_px = self.price(symbol)
        px = raw_px * (1 - self.cfg.slippage) if side == "long" else raw_px * (1 + self.cfg.slippage)
        fee = qty * px * self.cfg.fee_rate
        return px, fee

    def place_stop(self, symbol: str, side: str, qty: float, stop: float):
        return None

    def cancel(self, symbol: str, order_id):
        pass

    def stop_status(self, symbol: str, order_id):
        return None


class BinanceBroker:
    is_paper = False

    def __init__(self, cfg: Config, testnet: bool):
        self.cfg = cfg
        key = cfg.testnet_api_key if testnet else cfg.api_key
        secret = cfg.testnet_api_secret if testnet else cfg.api_secret
        if not key or not secret:
            raise SystemExit("Faltan las claves API en el archivo .env")
        opts = {"enableRateLimit": True, "apiKey": key, "secret": secret,
                "options": {"adjustForTimeDifference": True}}
        if cfg.market_type == "futures":
            self.ex = ccxt.binanceusdm(opts)
        else:
            opts["options"]["defaultType"] = "spot"
            self.ex = ccxt.binance(opts)
        if testnet:
            self.ex.set_sandbox_mode(True)
        self.ex.load_markets()
        self._configured_syms: set[str] = set()

    def _ensure_futures_settings(self, symbol: str):
        if self.cfg.market_type != "futures" or symbol in self._configured_syms:
            return
        try:
            self.ex.set_margin_mode(self.cfg.margin_mode, symbol)
        except Exception as e:
            log.debug(f"set_margin_mode {symbol}: {e}")
        try:
            self.ex.set_leverage(self.cfg.leverage, symbol)
        except Exception as e:
            log.debug(f"set_leverage {symbol}: {e}")
        self._configured_syms.add(symbol)

    def tradable_symbols(self):
        if self.cfg.market_type == "futures":
            return {s for s, m in self.ex.markets.items() if m.get("swap") and m.get("linear") and m.get("active")}
        return {s for s, m in self.ex.markets.items() if m.get("spot") and m.get("active")}

    def price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])

    def usdt_free(self) -> float:
        return float(self.ex.fetch_balance()["free"].get("USDT", 0) or 0)

    def _settle(self, order, symbol):
        if order.get("status") != "closed" or not order.get("filled"):
            order = self.ex.fetch_order(order["id"], symbol)
        return order

    @staticmethod
    def _fees_usdt(order) -> float:
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        return sum(float(f.get("cost") or 0) for f in fees if f and f.get("currency") == "USDT")

    def open_order(self, symbol: str, side: str, usdt: float):
        self._ensure_futures_settings(symbol)
        px = self.price(symbol)
        qty = float(self.ex.amount_to_precision(symbol, usdt / px))
        order_side = "buy" if side == "long" else "sell"
        o = self._settle(self.ex.create_order(symbol, "market", order_side, qty), symbol)
        filled = float(o["filled"])
        cost = float(o["cost"])
        avg_px = float(o.get("average") or (cost / filled))
        fee = self._fees_usdt(o)
        return filled, cost, fee, avg_px

    def close_order(self, symbol: str, side: str, qty: float):
        amount = float(self.ex.amount_to_precision(symbol, qty))
        order_side = "sell" if side == "long" else "buy"
        params = {"reduceOnly": True} if self.cfg.market_type == "futures" else {}
        o = self._settle(self.ex.create_order(symbol, "market", order_side, amount, None, params), symbol)
        filled = float(o["filled"])
        cost = float(o["cost"])
        avg_px = float(o.get("average") or (cost / filled))
        return avg_px, self._fees_usdt(o)

    def place_stop(self, symbol: str, side: str, qty: float, stop: float):
        amount = float(self.ex.amount_to_precision(symbol, qty))
        sp = float(self.ex.price_to_precision(symbol, stop))
        close_side = "sell" if side == "long" else "buy"
        if self.cfg.market_type == "futures":
            o = self.ex.create_order(
                symbol, "STOP_MARKET", close_side, amount, None,
                {"stopPrice": sp, "reduceOnly": True, "workingType": "MARK_PRICE"},
            )
            return o["id"]
        lp = float(self.ex.price_to_precision(symbol, stop * (1 - self.cfg.stop_limit_buffer)))
        o = self.ex.create_order(symbol, "STOP_LOSS_LIMIT", close_side, amount, lp,
                                 {"stopPrice": sp, "timeInForce": "GTC"})
        return o["id"]

    def cancel(self, symbol: str, order_id):
        if not order_id:
            return
        try:
            self.ex.cancel_order(order_id, symbol)
        except ccxt.OrderNotFound:
            pass

    def stop_status(self, symbol: str, order_id):
        if not order_id:
            return None
        try:
            return self.ex.fetch_order(order_id, symbol)
        except ccxt.OrderNotFound:
            return None

"""Datos de mercado públicos de Binance Spot / Futures (no requieren claves)."""
import os
import time

import ccxt
import pandas as pd

from .config import Config

STABLES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "EUR", "EURI", "AEUR", "USDE", "USD1",
    "PYUSD", "XUSD", "GBP", "TRY", "BRL", "UST", "USTC", "WBTC", "WBETH", "BFUSD", "RLUSD", "PAXG",
    "USDS", "BNSOL", "FRAX", "LUSD", "SUSD", "GUSD", "U",
}
NON_CRYPTO = {
    "XAU", "XAG", "XPT", "XPD", "COPPER", "CL", "BZ", "NG",
    "SPCX", "SPY", "QQQ", "TQQQ", "SOXL", "SOXS", "NVDA", "NVDAB",
    "AMD", "INTC", "GOOGL", "MSTR", "MU", "MUU", "SNDK", "SNDKB", "SNXX", "SOXLB", "MUB",
    "SAMSUNG", "SKHYNIX", "SKHY", "EWY", "KORU", "BABA", "COIN", "AVGO", "CRCL", "NBIS",
    "ZHONGJI", "DRAM", "TSLA", "AAPL", "MSFT", "AMZN", "META", "PLTR", "HOOD", "MSTRB",
    "HK1810", "NATGAS",
}
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def public_exchange(market_type: str = "futures") -> ccxt.Exchange:
    if market_type == "futures":
        return ccxt.binanceusdm({"enableRateLimit": True})
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def select_universe(ex: ccxt.Exchange, cfg: Config, allowed: set | None = None) -> list:
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for sym, m in markets.items():
        if cfg.market_type == "futures":
            ok = m.get("swap") and m.get("linear") and m.get("active") and m.get("quote") == cfg.quote
        else:
            ok = m.get("spot") and m.get("active") and m.get("quote") == cfg.quote
        if not ok:
            continue
        base = m["base"]
        if base in STABLES or base in NON_CRYPTO or base.endswith(("DOWN", "BULL", "BEAR")) or (base.endswith("UP") and len(base) > 4):
            continue
        if allowed is not None and sym not in allowed:
            continue
        qv = (tickers.get(sym) or {}).get("quoteVolume") or 0
        if qv >= cfg.min_quote_volume:
            rows.append((sym, qv))
    rows.sort(key=lambda r: r[1], reverse=True)
    universe = [s for s, _ in rows[: cfg.universe_size]]
    if cfg.btc_symbol not in universe and cfg.btc_symbol in markets:
        universe.insert(0, cfg.btc_symbol)
    return universe


def _to_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop(columns="ts").astype(float)


def fetch_recent(ex: ccxt.Exchange, symbol: str, tf: str = "1h", limit: int = 1000, include_live: bool = False) -> pd.DataFrame:
    """Últimas velas. Si include_live=False descarta la vela en formación."""
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = _to_df(raw)
    tf_ms = ex.parse_timeframe(tf) * 1000
    if not include_live and len(df) and (df.index[-1].value // 10**6) + tf_ms > ex.milliseconds():
        df = df.iloc[:-1]
    return df


def fetch_history(ex: ccxt.Exchange, symbol: str, days: int, tf: str = "1h", use_cache: bool = True) -> pd.DataFrame:
    os.makedirs(DATA_DIR, exist_ok=True)
    safe_sym = symbol.replace("/", "_").replace(":", "_")
    path = os.path.join(DATA_DIR, f"{safe_sym}_{tf}_{days}d.csv")
    if use_cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    tf_ms = ex.parse_timeframe(tf) * 1000
    since = ex.milliseconds() - days * 86400 * 1000
    out = []
    while True:
        batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        out += batch
        since = batch[-1][0] + tf_ms
        if len(batch) < 1000:
            break
    df = _to_df(out)
    tf_ms_now = ex.milliseconds()
    if len(df) and (df.index[-1].value // 10**6) + tf_ms > tf_ms_now:
        df = df.iloc[:-1]
    df.to_csv(path)
    return df

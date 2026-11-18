"""Configuración central del bot. Todo se puede sobreescribir desde el archivo .env"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "si", "sí")


@dataclass
class Config:
    # --- Credenciales (solo en .env, NUNCA en el código) ---
    api_key: str = ""
    api_secret: str = ""
    testnet_api_key: str = ""
    testnet_api_secret: str = ""

    # --- Mercado ---
    market_type: str = "futures"       # "futures" (long + short) o "spot" (solo long)
    leverage: int = 1                  # 1x por defecto (sin apalancamiento extra)
    margin_mode: str = "isolated"      # "isolated" o "cross"
    allow_short: bool = True           # habilitar cortos en modo futures
    timeframe: str = "1h"
    quote: str = "USDT"
    universe_size: int = 40            # top N monedas por volumen
    min_quote_volume: float = 50e6     # volumen mínimo 24h en USDT (liquidez)
    btc_symbol: str = "BTC/USDT:USDT"  # en spot se ajusta automáticamente a BTC/USDT

    # --- Estrategia ---
    top_n_rank: int = 8                # top N (long) / bottom N (short) del ranking
    rsi_max: float = 75.0              # no perseguir longs sobrecomprados
    rsi_min: float = 25.0              # no perseguir shorts sobrevendidos
    max_ext_atr: float = 2.5           # máx. distancia a la EMA20 (en ATRs) para entrar
    regime_ema: int = 800              # EMA de 800h (~EMA200 en 4h) sobre BTC = filtro de mercado
    min_breadth: float = 0.40          # >= 40% sobre EMA200 para LONG; <= 40% para SHORT

    # --- Riesgo ---
    risk_per_trade: float = 0.01       # 1% del capital arriesgado por operación
    max_positions: int = 4
    max_position_pct: float = 0.25     # máx. 25% del capital en una sola moneda
    atr_stop_mult: float = 2.5         # stop inicial = 2.5*ATR
    atr_trail_mult: float = 3.0        # trailing stop = 3*ATR desde máximo/mínimo
    max_daily_loss: float = 0.03       # -3% en el día => no más entradas hoy
    max_drawdown: float = 0.20         # -20% desde el máximo => el bot se detiene
    stop_limit_buffer: float = 0.01    # buffer para stop-limit en spot

    # --- Costes (para backtest y modo papel) ---
    fee_rate: float = 0.0005           # 0.05% comisión taker Binance Futures (0.001 en spot)
    slippage: float = 0.0005
    min_notional: float = 10.0

    # --- Capital ---
    paper_capital: float = 1000.0      # capital ficticio para modo papel
    max_capital: float = 100.0         # MÁXIMO de USDT que el bot puede usar en modo real/testnet
    account_balance: float = 228.0     # capital manual para calcular tamaño sugerido por señal
    manual_risk_pct: float = 0.02      # riesgo por operación manual (2% = ~4.56 con 228)


def load_config() -> Config:
    c = Config()
    c.api_key = os.getenv("BINANCE_API_KEY", "")
    c.api_secret = os.getenv("BINANCE_API_SECRET", "")
    c.testnet_api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
    c.testnet_api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
    c.market_type = os.getenv("MARKET_TYPE", c.market_type).strip().lower()
    if c.market_type == "spot":
        c.btc_symbol = "BTC/USDT"
        c.allow_short = False
        c.fee_rate = 0.001
        c.min_quote_volume = 20e6
    else:
        c.btc_symbol = "BTC/USDT:USDT"
    c.leverage = _i("LEVERAGE", c.leverage)
    c.allow_short = _b("ALLOW_SHORT", c.allow_short)
    c.universe_size = _i("UNIVERSE_SIZE", c.universe_size)
    c.min_quote_volume = _f("MIN_QUOTE_VOLUME", c.min_quote_volume)
    c.top_n_rank = _i("TOP_N_RANK", c.top_n_rank)
    c.risk_per_trade = _f("RISK_PER_TRADE", c.risk_per_trade)
    c.max_positions = _i("MAX_POSITIONS", c.max_positions)
    c.max_position_pct = _f("MAX_POSITION_PCT", c.max_position_pct)
    c.max_daily_loss = _f("MAX_DAILY_LOSS", c.max_daily_loss)
    c.max_drawdown = _f("MAX_DRAWDOWN", c.max_drawdown)
    c.paper_capital = _f("PAPER_CAPITAL", c.paper_capital)
    c.max_capital = _f("MAX_CAPITAL", c.max_capital)
    c.account_balance = _f("ACCOUNT_BALANCE", c.account_balance)
    c.manual_risk_pct = _f("MANUAL_RISK_PCT", c.manual_risk_pct)
    return c

import unittest
import numpy as np
import pandas as pd

from tradingbot.backtest import run_backtest
from tradingbot.config import Config
from tradingbot.indicators import atr, ema, rsi, trend_quality
from tradingbot.strategy import build_panel, compute_features, compute_scores, initial_stop, update_stop


def _make_ohlcv(n=1000, drift=0.0005, seed=0):
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, 0.008, n)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.roll(close, 1)
    open_[0] = 100.0
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.005, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.005, n))
    vol = rng.uniform(1e5, 5e5, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


class TestStrategy(unittest.TestCase):
    def test_indicators_shapes_and_bounds(self):
        df = _make_ohlcv(300)
        e = ema(df["close"], 20)
        r = rsi(df["close"], 14)
        a = atr(df, 14)
        slope, r2 = trend_quality(df["close"], 72)
        self.assertEqual(len(e), 300)
        self.assertTrue((r.dropna() >= 0).all() and (r.dropna() <= 100).all())
        self.assertTrue((a.dropna() > 0).all())
        self.assertTrue((r2.dropna() >= 0).all() and (r2.dropna() <= 1.000001).all())
        self.assertTrue(slope.dropna().notna().all())

    def test_trailing_stop_long_and_short(self):
        cfg = Config()
        # LONG
        s0 = initial_stop(100.0, 2.0, cfg, side="long")
        self.assertAlmostEqual(s0, 95.0)
        pos_l = {"side": "long", "entry": 100.0, "stop": s0, "initial_stop": s0, "highest": 100.0}
        s1 = update_stop(pos_l, high=106.0, close=105.5, atr_value=2.0, cfg=cfg)
        self.assertGreaterEqual(s1, 100.0 * (1 + 2 * cfg.fee_rate))
        s2 = update_stop(pos_l, high=102.0, close=101.0, atr_value=2.0, cfg=cfg)
        self.assertEqual(s2, s1)

        # SHORT
        sh0 = initial_stop(100.0, 2.0, cfg, side="short")
        self.assertAlmostEqual(sh0, 105.0)
        pos_s = {"side": "short", "entry": 100.0, "stop": sh0, "initial_stop": sh0, "lowest": 100.0}
        sh1 = update_stop(pos_s, high=100.0, low=94.0, close=94.5, atr_value=2.0, cfg=cfg)
        self.assertLessEqual(sh1, 100.0 * (1 - 2 * cfg.fee_rate))
        sh2 = update_stop(pos_s, high=98.0, low=96.0, close=97.0, atr_value=2.0, cfg=cfg)
        self.assertEqual(sh2, sh1)

    def test_backtest_runs_long_and_short(self):
        cfg = Config()
        cfg.regime_ema = 200
        cfg.btc_symbol = "BTC/USDT:USDT"
        data = {
            "BTC/USDT:USDT": _make_ohlcv(900, drift=-0.0006, seed=1),
            "ETH/USDT:USDT": _make_ohlcv(900, drift=-0.0009, seed=2),
            "SOL/USDT:USDT": _make_ohlcv(900, drift=-0.0014, seed=3),
            "ADA/USDT:USDT": _make_ohlcv(900, drift=0.0003, seed=4),
        }
        feats = {s: compute_features(df, cfg) for s, df in data.items()}
        panel = build_panel(feats)
        sc = compute_scores(panel, cfg)
        self.assertIn("SOL/USDT:USDT", sc["rank_short"].columns)
        res = run_backtest(data, cfg, capital=1000.0)
        self.assertGreater(len(res["equity"]), 100)
        self.assertGreater(res["equity"].iloc[-1], 0)


if __name__ == "__main__":
    unittest.main()

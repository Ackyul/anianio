"""Punto de entrada CLI del bot de trading."""
import argparse
import json
import logging
import os
import sys

from tradingbot.analyzer import analyze_market, format_analysis
from tradingbot.backtest import report, run_backtest, save_results
from tradingbot.bot import Bot, STATE_DIR, LOG_DIR
from tradingbot.config import load_config
from tradingbot.market_data import fetch_history, fetch_recent, public_exchange, select_universe
from tradingbot.strategy import build_panel, compute_features, compute_scores
from tradingbot.telegram_bot import run_telegram_bot


def _setup_logging(mode: str | None = None):
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if mode:
        os.makedirs(LOG_DIR, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(LOG_DIR, f"bot_{mode}.log"), encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def cmd_backtest(args):
    cfg = load_config()
    ex = public_exchange(cfg.market_type)
    print(f"Mercado: {cfg.market_type.upper()} (Long+Short: {'SÍ' if cfg.allow_short else 'NO'})")
    print(f"Seleccionando universo (top {cfg.universe_size} pares USDT por volumen)...")
    universe = select_universe(ex, cfg)
    print(f"Universo ({len(universe)}): {', '.join(s.split('/')[0] for s in universe[:15])}...")
    print(f"Descargando historial de {args.days} días ({cfg.timeframe})...")
    data = {}
    for i, sym in enumerate(universe, 1):
        try:
            df = fetch_history(ex, sym, days=args.days, tf=cfg.timeframe, use_cache=not args.no_cache)
            if len(df) > 300:
                data[sym] = df
            print(f"  [{i}/{len(universe)}] {sym}: {len(df)} velas", end="\r")
        except Exception as e:
            print(f"\n  Aviso: no se pudo descargar {sym}: {e}")
    print("\nEjecutando backtest...")
    res = run_backtest(data, cfg, capital=args.capital)
    print(report(res))
    out_dir = save_results(res)
    print(f"Resultados guardados en: {out_dir}")


def _print_table(title, rank_row, score_row, feats, sig_col, trend_col, top_n, side_cond):
    print(title)
    print(f"{'Rank':<5} {'Par':<16} {'Precio':>12} {'Score':>8} {'RSI':>6} {'Tend.':>6} {'Señal':>7}")
    print("-" * 74)
    for sym in rank_row.dropna().sort_values().index[:top_n]:
        r = int(rank_row[sym])
        f = feats[sym].iloc[-1]
        sc = score_row[sym]
        sig = "ENTRADA" if f[sig_col] > 0 and side_cond(sc) and r <= 8 else "-"
        trend = "OK" if f[trend_col] > 0 else "NO"
        print(f"{r:<5} {sym:<16} {f['close']:>12.4g} {sc:>+8.2f} {f['rsi']:>6.1f} {trend:>6} {sig:>7}")


def cmd_scan(args):
    cfg = load_config()
    ex = public_exchange(cfg.market_type)
    print(f"Escaneando mercado Binance {cfg.market_type.upper()}...")
    universe = select_universe(ex, cfg)
    feats = {}
    for sym in universe:
        try:
            df = fetch_recent(ex, sym, cfg.timeframe, 1000)
            if len(df) > 300:
                feats[sym] = compute_features(df, cfg)
        except Exception as e:
            print(f"  Sin datos para {sym}: {e}")
    panel = build_panel(feats)
    sc = compute_scores(panel, cfg)
    bull = bool(sc["bull_on"].iloc[-1])
    bear = bool(sc["bear_on"].iloc[-1])
    btc_ok = bool(sc["btc_ok"].iloc[-1])
    breadth = float(sc["breadth"].iloc[-1])
    score_row = sc["score"].iloc[-1]
    reg_txt = "ALCISTA (habilita LONG)" if bull else ("BAJISTA (habilita SHORT)" if bear else "MIXTO (esperar)")

    print("\n" + "=" * 74)
    print(f"RÉGIMEN DE MERCADO: {reg_txt}")
    print(f"  BTC > EMA{cfg.regime_ema}: {'SÍ' if btc_ok else 'NO'}   |   "
          f"Amplitud (>EMA200): {breadth:.1%} (umbral {cfg.min_breadth:.0%})")
    print("=" * 74)
    _print_table("--- TOP CANDIDATAS LONG (más fuertes) ---",
                 sc["rank"].iloc[-1], score_row, feats, "long_sig", "trend_long", args.top, lambda s: s > 0)
    if cfg.allow_short:
        print()
        _print_table("--- TOP CANDIDATAS SHORT (más débiles) ---",
                     sc["rank_short"].iloc[-1], score_row, feats, "short_sig", "trend_short", args.top, lambda s: s < 0)
    print("=" * 74)


def cmd_signals(args):
    cfg = load_config()
    label = "MODO NOCTURNO SEGURO (top 30, 4h alineado, SL <= 4%)" if args.safe else f"top {args.universe} monedas"
    print(f"Analizando {label} de Binance Futures...")
    res = analyze_market(cfg, universe_limit=args.universe, top_picks=args.top, safe_mode=args.safe)
    print(format_analysis(res))


def cmd_telegram(args):
    _setup_logging("telegram")
    cfg = load_config()
    run_telegram_bot(cfg)


def cmd_run(args):
    _setup_logging(args.mode)
    cfg = load_config()
    if args.mode == "live" and not args.confirm_live:
        print(f"ATENCIÓN: Vas a iniciar el bot con DINERO REAL en Binance ({cfg.market_type.upper()}).")
        print(f"Capital máximo configurado (MAX_CAPITAL): {cfg.max_capital:.2f} USDT | Apalancamiento: {cfg.leverage}x")
        ans = input("Escribe 'SI OPERAR REAL' para continuar: ").strip()
        if ans != "SI OPERAR REAL":
            print("Cancelado.")
            return
    bot = Bot(cfg, mode=args.mode)
    bot.run()


def cmd_status(args):
    cfg = load_config()
    path = os.path.join(STATE_DIR, f"{args.mode}_{cfg.market_type}.json")
    if not os.path.exists(path):
        print(f"No hay estado guardado para '{args.mode}' ({cfg.market_type}).")
        return
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    print(json.dumps(st, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="TradingBot - Rotación por Momentum Long/Short en Binance")
    sub = p.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="Simular estrategia con datos históricos reales de Binance")
    bt.add_argument("--days", type=int, default=180, help="Días de historia a descargar (defecto: 180)")
    bt.add_argument("--capital", type=float, default=1000.0, help="Capital inicial en USDT (defecto: 1000)")
    bt.add_argument("--no-cache", action="store_true", help="Forzar descarga fresca sin usar caché local")
    bt.set_defaults(func=cmd_backtest)

    sc = sub.add_parser("scan", help="Escanear el mercado ahora y mostrar rankings Long y Short")
    sc.add_argument("--top", type=int, default=10, help="Cuántas monedas mostrar por lado (defecto: 10)")
    sc.set_defaults(func=cmd_scan)

    sg = sub.add_parser("signals", help="Analizador completo para operar manual: Entrada, SL y TP")
    sg.add_argument("--universe", type=int, default=200, help="Cuántas monedas líquidas analizar (defecto: 200)")
    sg.add_argument("--top", type=int, default=5, help="Máximo de señales por lado (defecto: 5)")
    sg.add_argument("--safe", action="store_true", help="Modo nocturno seguro: solo a favor del régimen, 4h alineado y SL <= 4%%")
    sg.set_defaults(func=cmd_signals)

    tg = sub.add_parser("telegram", help="Iniciar bot de Telegram para consultar señales desde tu iPhone")
    tg.set_defaults(func=cmd_telegram)

    rn = sub.add_parser("run", help="Ejecutar el bot en bucle continuo")
    rn.add_argument("--mode", choices=["paper", "testnet", "live"], default="paper",
                    help="Modo: paper (simulado), testnet (Binance Testnet) o live (dinero real)")
    rn.add_argument("--confirm-live", action="store_true", help="Confirmar modo live sin prompt")
    rn.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="Ver el estado guardado del bot")
    st.add_argument("--mode", choices=["paper", "testnet", "live"], default="paper")
    st.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

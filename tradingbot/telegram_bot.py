"""Bot de Telegram para consultar señales y vigilar oportunidades desde iPhone/Android."""
import logging
import os
import time

import requests

from .analyzer import analyze_market, format_analysis
from .config import Config
from .tracker import format_summary, record_signals

log = logging.getLogger("telegram")


def _send(token: str, chat_id: str | int, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 3800):
        requests.post(url, json={"chat_id": chat_id, "text": text[i:i + 3800]}, timeout=15)


def _format_alerts(picks: list[dict], res: dict) -> str:
    lines = [
        f"ALERTA DE SEÑAL (>= 6.0/10) | Mercado: {res['regime']}",
        "-" * 50,
    ]
    for i, p in enumerate(picks, 1):
        pair = p["symbol"].split(":")[0].replace("/", "")
        lines.append(f"\n{i}. {pair}  ->  {p['side']}  (Seguridad: {p['safety']:.1f}/10)")
        lines.append(f"   Entrada:     {p['entry']:.6g} (precio actual)")
        lines.append(f"   Take Profit: {p['tp']:.6g}  (+{p['tp_pct']:.2%})")
        lines.append(f"   Stop Loss:   {p['sl']:.6g}  (-{p['sl_pct']:.2%})")
        lines.append(f"   Tamaño (2x): Pon {p['margin_2x']:.1f} de margen (orden {p['notional']:.1f} | riesgo -{p['risk_usdt']:.2f})")
        lines.append(f"   Motivo:      {', '.join(p['reasons'])}")
    return "\n".join(lines)


def run_telegram_bot(cfg: Config):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en el archivo .env")

    base = f"https://api.telegram.org/bot{token}"
    offset = 0
    watch_chat: str | None = None
    last_watch_ts = 0.0
    alerted: dict[str, float] = {}
    log.info("Bot de Telegram iniciado. Envia /senales, /noche o /vigilar desde tu iPhone.")

    while True:
        try:
            r = requests.get(f"{base}/getUpdates", params={"offset": offset, "timeout": 10}, timeout=20)
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip().lower()
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not text or not chat_id:
                    continue
                if allowed_chat and chat_id != allowed_chat:
                    log.warning(f"Mensaje ignorado de chat no autorizado: {chat_id}")
                    continue
                if not allowed_chat:
                    log.info(f"Tu TELEGRAM_CHAT_ID es: {chat_id} (puedes fijarlo en .env)")

                if text in ("/start", "/ayuda", "/help"):
                    _send(token, chat_id,
                          "Comandos disponibles:\n"
                          "/senales - Analisis en vivo de Binance ahora\n"
                          "/noche   - Modo nocturno seguro (antes de dormir)\n"
                          "/vigilar - Revisa cada 15 min y te avisa si sale algo >= 6.0/10\n"
                          "/resumen - Ver cuantas señales ganaron (TP) o perdieron (SL)\n"
                          "/capital 250 - Actualizar tu saldo para calcular el tamaño\n"
                          "/parar   - Detiene las alertas automaticas")
                elif text.startswith("/capital"):
                    parts = text.split()
                    if len(parts) == 2:
                        try:
                            cfg.account_balance = float(parts[1])
                            _send(token, chat_id, f"Capital actualizado a {cfg.account_balance:.2f} (riesgo 2% = {cfg.account_balance * cfg.manual_risk_pct:.2f} por trade).")
                        except ValueError:
                            _send(token, chat_id, "Usa por ejemplo: /capital 250")
                    else:
                        _send(token, chat_id, f"Capital actual configurado: {cfg.account_balance:.2f}. Para cambiarlo escribe /capital 250")
                elif text == "/resumen":
                    try:
                        _send(token, chat_id, format_summary())
                    except Exception as e:
                        log.exception("Error en /resumen")
                        _send(token, chat_id, f"Error generando resumen: {e}")
                elif text in ("/senales", "/señales", "/signals"):
                    _send(token, chat_id, "Analizando Binance Futures en vivo, espera ~15 segundos...")
                    try:
                        res = analyze_market(cfg, universe_limit=80, top_picks=3, safe_mode=False)
                        record_signals(res["longs"] + res["shorts"], source="senales")
                        _send(token, chat_id, format_analysis(res))
                    except Exception as e:
                        log.exception("Error en /senales")
                        _send(token, chat_id, f"Error al analizar el mercado: {e}")
                elif text in ("/noche", "/safe"):
                    _send(token, chat_id, "Analizando Modo Nocturno Seguro en Binance, espera ~15 segundos...")
                    try:
                        res = analyze_market(cfg, universe_limit=60, top_picks=2, safe_mode=True)
                        record_signals(res["longs"] + res["shorts"], source="noche")
                        _send(token, chat_id, format_analysis(res))
                    except Exception as e:
                        log.exception("Error en /noche")
                        _send(token, chat_id, f"Error al analizar el mercado: {e}")
                elif text == "/vigilar":
                    watch_chat = chat_id
                    last_watch_ts = 0.0
                    _send(token, chat_id, "Vigilancia activada: revisare Binance cada 15 min y te avisare cuando salga una señal >= 6.0/10. Envia /parar para detener.")
                elif text == "/parar":
                    watch_chat = None
                    _send(token, chat_id, "Vigilancia detenida.")

            now = time.time()
            if watch_chat and (now - last_watch_ts >= 900):
                last_watch_ts = now
                try:
                    res = analyze_market(cfg, universe_limit=80, top_picks=5, safe_mode=False)
                    good = [p for p in (res["longs"] + res["shorts"]) if p.get("safety", 0) >= 6.0]
                    fresh = [p for p in good if now - alerted.get(f"{p['symbol']}_{p['side']}", 0) >= 3600]
                    if fresh:
                        fresh.sort(key=lambda p: p["safety"], reverse=True)
                        for p in fresh:
                            alerted[f"{p['symbol']}_{p['side']}"] = now
                        record_signals(fresh[:3], source="vigilar")
                        _send(token, watch_chat, _format_alerts(fresh[:3], res))
                except Exception as e:
                    log.warning(f"Error en vigilancia automatica: {e}")
        except requests.RequestException as e:
            log.warning(f"Error de red Telegram: {e}")
            time.sleep(5)
        except Exception:
            log.exception("Error inesperado en Telegram bot")
            time.sleep(5)

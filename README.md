# TradingBot (Binance Spot - Rotación por Momentum)

Bot cuantitativo para **Binance Spot** que selecciona automáticamente las monedas con mejor momentum y fuerza relativa, aplica un filtro de régimen de mercado y gestiona el riesgo con stops en el exchange.

## Cómo decide en qué monedas entrar

1. **Filtro de régimen de mercado (protección en caídas):**
   Solo abre compras nuevas cuando `BTC/USDT` está por encima de su EMA de 800 horas (~EMA200 en 4h) **y** al menos el 40% de las monedas líquidas del universo están sobre su EMA200. En mercados bajistas se queda en USDT.
2. **Universo líquido:**
   Toma los 40 pares contra `USDT` con mayor volumen en 24h (mínimo 20 millones de USDT/día), excluyendo stablecoins y tokens apalancados.
3. **Ranking cuantitativo (cada hora):**
   Calcula un *score* combinado por z-scores:
   - **40%** Momentum ajustado por volatilidad (retornos 24h, 72h y 7d / volatilidad).
   - **25%** Fuerza relativa frente a BTC (7 días).
   - **20%** Calidad de tendencia (pendiente × R² sobre 72h).
   - **15%** Aumento de volumen (media 24h vs 7d).
4. **Gatillo de entrada:**
   Solo compra si la moneda está en el **Top 8**, en tendencia alcista (`EMA20 > EMA50 > EMA200`), no sobreextendida (`RSI < 75` y `< 2.5 ATR` sobre la `EMA20`), y hace una ruptura de 20 velas o rebote alcista en la `EMA20`.
5. **Gestión de riesgo y salidas:**
   - Riesgo máximo del **1% del capital por operación** (tamaño calculado según el ATR).
   - Máximo **4 posiciones simultáneas** (máx. 25% del capital en una moneda).
   - Stop-loss inicial (`2.5 ATR`), *break-even* al alcanzar `+1R`, *trailing stop* (`3 ATR` desde el máximo) y salida si el precio cierra bajo la `EMA50`.
   - En modo `testnet` o `live`, coloca órdenes `STOP_LOSS_LIMIT` reales en Binance.
   - Límite de pérdida diaria (`-3%`) y *drawdown* máximo (`-20%`).

## Instalación y uso

```powershell
# 1. Activar entorno virtual (ya creado)
.venv\Scripts\Activate.ps1

# 2. Escanear el mercado ahora mismo (solo lectura, sin claves)
python main.py scan --top 15

# 3. Correr un backtest con datos históricos reales de Binance
python main.py backtest --days 180 --capital 1000

# 4. Ejecutar en modo papel (dinero ficticio, precios reales)
python main.py run --mode paper
```

## Pasar a Testnet o Dinero Real

1. Copia `.env.example` a `.env` y rellena tus claves API (NUNCA compartas `.env`).
2. Para dinero real en Binance:
   - Activa **solo** *Enable Spot & Margin Trading*.
   - **Nunca** actives retiros (*Enable Withdrawals*).
   - Restringe la API key a la IP de tu PC.
   - Ajusta `MAX_CAPITAL` en `.env` (por defecto `100.0` USDT).
3. Prueba primero varios días en `testnet`:
   ```powershell
   python main.py run --mode testnet
   ```
4. Cuando estés listo para real:
   ```powershell
   python main.py run --mode live
   ```

## Controles de emergencia

- Crea un archivo vacío llamado `STOP` en la carpeta del proyecto para pausar nuevas compras (mantiene y gestiona las abiertas).
- Crea un archivo vacío llamado `PANIC` para vender inmediatamente todas las posiciones abiertas del bot y apagarlo.

## Despliegue 24/7 en VPS Linux (Ubuntu/Debian fuera de EE. UU.)

1. Contrata o crea un VPS en **Europa (Frankfurt/Amsterdam)** o **Sudamérica (São Paulo)** para evitar bloqueos regionales de Binance.
2. Sube la carpeta del proyecto (sin `.venv` ni `.env`) al VPS, por ejemplo en `~/TradingBot`.
3. Ejecuta el instalador incluido:
   ```bash
   cd ~/TradingBot
   chmod +x deploy/setup_vps.sh
   ./deploy/setup_vps.sh
   ```
4. Coloca tu `TELEGRAM_BOT_TOKEN` en `~/TradingBot/.env`:
   ```bash
   nano ~/TradingBot/.env
   ```
5. Enciende el servicio 24/7 y revisa los logs:
   ```bash
   sudo systemctl start tradingbot-telegram
   sudo journalctl -u tradingbot-telegram -f
   ```

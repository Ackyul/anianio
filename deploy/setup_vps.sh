#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(whoami)"

echo "=== Instalando dependencias del sistema y Python venv en $APP_DIR ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "ATENCION: Edita $APP_DIR/.env con tu TELEGRAM_BOT_TOKEN antes de iniciar el servicio."
fi

SERVICE_FILE="/etc/systemd/system/tradingbot-telegram.service"
echo "=== Creando servicio systemd ($SERVICE_FILE) ==="
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=TradingBot Telegram Signals Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py telegram
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable tradingbot-telegram
echo "=== Listo. Ahora edita .env y ejecuta: sudo systemctl start tradingbot-telegram ==="

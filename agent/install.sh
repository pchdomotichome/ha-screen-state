#!/usr/bin/env bash
# Instala pc-screen-agent como servicio de usuario de systemd.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/pc-screen-agent"
CONF_DIR="$HOME/.config/pc-screen-agent"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$APP_DIR" "$CONF_DIR" "$UNIT_DIR"
install -m 755 "$SRC/pc-screen-agent.py" "$APP_DIR/pc-screen-agent.py"
install -m 644 "$SRC/pc-screen-agent.service" "$UNIT_DIR/pc-screen-agent.service"

if [[ ! -f "$CONF_DIR/agent.env" ]]; then
  install -m 600 "$SRC/agent.env.example" "$CONF_DIR/agent.env"
  echo "-> Crea la configuración en: $CONF_DIR/agent.env"
fi

systemctl --user daemon-reload

if grep -q "PEGA_AQUI_TU_TOKEN" "$CONF_DIR/agent.env"; then
  echo "-> Configuración pendiente: completa $CONF_DIR/agent.env (HOST_SLUG, HA_URL, HA_TOKEN)"
  echo "   y luego ejecuta: systemctl --user enable --now pc-screen-agent"
else
  systemctl --user enable --now pc-screen-agent
  systemctl --user restart pc-screen-agent
  echo "-> Agente instalado y arrancado."
  echo "   Estado:  curl -s http://127.0.0.1:$(grep -E '^AGENT_PORT=' "$CONF_DIR/agent.env" | cut -d= -f2)/state"
  echo "   Logs:    tail -f $APP_DIR/agent.log"
fi
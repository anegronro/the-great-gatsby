#!/usr/bin/env bash
# Sirve "The Great Gatsby" (acciones, puerto 8765) en el tailnet de Tailscale,
# para verla desde el teléfono. Bindea SOLO a la IP de Tailscale — no a 0.0.0.0 —
# así queda dentro de tu red privada y NO expuesta al WiFi público.
#
# Uso:   cd engine && ./scripts/serve_tailnet.sh
# El teléfono (mismo tailnet) abre:  http://<IP-de-tailscale>:8765
#
# Es el agente de ACCIONES. El forex es otra app en otro puerto (8766); no se
# mezclan.
set -euo pipefail

cd "$(dirname "$0")/.."   # -> engine/

TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
[ -x "$TS_BIN" ] || TS_BIN="$(command -v tailscale || true)"
[ -n "$TS_BIN" ] || { echo "No encuentro el binario de Tailscale." >&2; exit 1; }

IP="$("$TS_BIN" ip -4 2>/dev/null | head -1)"
[ -n "$IP" ] || { echo "Tailscale no está activo (sin IP). Enciéndelo y reintenta." >&2; exit 1; }

export WBJ_HOST="$IP"
export WBJ_PORT="${WBJ_PORT:-8765}"

echo "The Great Gatsby en el tailnet -> http://$IP:$WBJ_PORT"
echo "Abre esa dirección desde el teléfono (con Tailscale encendido)."
exec .venv/bin/python scripts/webapp.py

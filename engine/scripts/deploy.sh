#!/usr/bin/env bash
# Guarda los cambios en GitHub y en el VPS (The Great Gatsby, agente de ACCIONES).
#
# Uso:  cd engine && ./scripts/deploy.sh
#
# 1) push de la rama actual a GitHub
# 2) rsync del código al VPS (SIN secretos: excluye API/, así el .env del VPS —
#    sin credenciales de Robinhood — no se pisa ni se re-suben llaves)
# 3) reinicia el servicio systemd `gatsby` en el VPS
set -euo pipefail

cd "$(dirname "$0")/.."                 # -> engine/
REPO_ROOT="$(cd .. && pwd)"
BRANCH="$(git -C "$REPO_ROOT" branch --show-current)"
GIT_REMOTE="${GATSBY_GIT_REMOTE:-mine}" # tu repo (anegronro), no el de Victor
VPS="root@100.105.244.125"
VPS_DIR="/root/gatsby"

echo "== 1/3  GitHub: push $BRANCH -> $GIT_REMOTE =="
git -C "$REPO_ROOT" push "$GIT_REMOTE" "$BRANCH"

echo "== 2/3  VPS: rsync del código (sin API/, venv, cache) =="
rsync -az \
  --exclude '.git/' --exclude 'engine/.venv/' --exclude 'engine/cache/' \
  --exclude 'engine/.pytest_cache/' --exclude '.pytest_cache/' \
  --exclude '**/__pycache__/' --exclude 'Reportes/' --exclude '.DS_Store' \
  --exclude 'API/' \
  "$REPO_ROOT/" "$VPS:$VPS_DIR/"

echo "== 3/3  VPS: reiniciar servicio gatsby =="
ssh "$VPS" 'systemctl restart gatsby && systemctl is-active gatsby'

echo "✅ Guardado en GitHub y VPS."

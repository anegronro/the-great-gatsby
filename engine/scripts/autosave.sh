#!/usr/bin/env bash
# Autoguardado del agente de ACCIONES (The Great Gatsby).
# Lo dispara launchd cada ~2 min. Si hay cambios en el repo: commitea y
# despliega (push a tu GitHub 'mine' + rsync al VPS + restart gatsby).
#
# Seguro: API/ y venv/cache están gitignoreados, así que nunca sube secretos.
# El log vive FUERA del repo (~/Library/Logs) para no auto-dispararse en bucle.

set -u

REPO="/Users/angel/Desktop/warren-buffett-jr"
LOG="$HOME/Library/Logs/gatsby-autosave.log"
LOCKDIR="/tmp/gatsby-autosave.lock"

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:$PATH"
ts() { date "+%Y-%m-%d %H:%M:%S"; }

# Un solo autosave a la vez (lock atómico portable — macOS no trae flock).
mkdir "$LOCKDIR" 2>/dev/null || exit 0
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

cd "$REPO" || exit 1

# ¿Hay algo que guardar? (respeta .gitignore)
[ -z "$(git status --porcelain)" ] && exit 0

git add -A
# Si tras el add no quedó nada staged (p.ej. solo archivos ignorados), salir.
git diff --cached --quiet && exit 0

# Solo commitea; el git hook post-commit dispara el deploy (push + VPS). Así hay
# un único camino de despliegue, venga el commit de aquí o de una edición manual.
echo "[$(ts)] cambios detectados -> commit" >> "$LOG"
git commit -q -m "autosave: $(ts)" >> "$LOG" 2>&1
echo "[$(ts)] commit hecho (el hook post-commit despliega)" >> "$LOG"

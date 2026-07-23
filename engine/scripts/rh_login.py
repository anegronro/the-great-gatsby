"""Login de Robinhood de UNA sola vez — crea la sesión que la webapp reutiliza.

Cómo se usa (una sola vez, en la terminal):

    cd engine && .venv/bin/python scripts/rh_login.py

Lee RH_USERNAME / RH_PASSWORD de API/.env (o los pide si faltan), inicia sesión
—tecleando el código MFA que Robinhood te envíe cuando lo pida— y guarda la
sesión en ~/.tokens/robinhood.pickle. Después de esto la webapp inicia sesión en
silencio reutilizando esa sesión, sin necesitar el secreto TOTP. Vuelve a correr
este script si la sesión expira (los pedidos de orden dirán "No hay sesión").

No hace nada autónomo y NO coloca ninguna orden: esto solo autentica.
"""

from __future__ import annotations

import sys
from getpass import getpass

import robin_stocks.robinhood as rh

from wbj.config import load_settings
from wbj.providers.robinhood import SESSION_FILE


def main() -> int:
    s = load_settings()
    user = s.rh_username or input("Robinhood usuario/email: ").strip()
    pw = s.rh_password or getpass("Robinhood contraseña: ")
    if not user or not pw:
        print("Faltan credenciales (RH_USERNAME / RH_PASSWORD).", file=sys.stderr)
        return 1

    print("Iniciando sesión en Robinhood…")
    print("Si te pide un código MFA (SMS o app), tecléalo aquí cuando aparezca.")
    try:
        rh.login(username=user, password=pw, store_session=True)
    except Exception as exc:  # noqa: BLE001 - CLI: report and exit non-zero
        print(f"\nFalló el login: {exc}", file=sys.stderr)
        return 1

    # Confirmación no sensible: leer poder de compra prueba que la sesión sirve.
    try:
        bp = rh.profiles.load_account_profile(info="buying_power")
    except Exception:  # noqa: BLE001
        bp = None

    if not SESSION_FILE.exists():
        print("\nSesión iniciada pero no se guardó el archivo esperado en "
              f"{SESSION_FILE}. Revisa permisos.", file=sys.stderr)
        return 1

    print(f"\n✅ Listo. Sesión guardada en {SESSION_FILE}")
    if bp is not None:
        print(f"   Poder de compra de la cuenta: ${bp}")
    print("   La webapp ya puede colocar órdenes (con tu clic). ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

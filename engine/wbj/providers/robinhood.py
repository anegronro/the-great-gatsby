"""Robinhood order-entry provider (unofficial API via robin_stocks).

This is the ONE place in the engine that can move real money, so it is written
to fail closed:

1. `place_long_with_stop` refuses unless `confirm is True`. A missing/false
   confirm raises `OrderRefused` before any network call — nothing autonomous
   can ever place an order, and neither can a stray call.
2. It refuses without a protective stop. "Sin stop no se abre la posición"
   (CLAUDE.md / perfil): the stop leg is placed right after the entry, and if
   the broker rejects it the result is flagged UNHEDGED, loudly — never a
   silent naked position.
3. Credentials are read from `Settings` (which loads them from API/.env, filled
   by Angel). This module never prints them and never asks for them.

The project rule still stands above this file: the human clicks Enviar. This
code is the plumbing behind that click, not a trader. It is long-only — the
profile trades US equities long (no shorting on a cash account); a sell ticket
is rejected here rather than mishandled.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Where robin_stocks stores the session after a `store_session=True` login.
# The web app never authenticates interactively — Angel runs scripts/rh_login.py
# once to create this file, and the app reuses it. If it is missing we refuse
# with guidance instead of triggering robin_stocks' interactive MFA prompt
# (which would hang a server thread).
SESSION_FILE = Path.home() / ".tokens" / "robinhood.pickle"

try:  # unofficial deps; absence simply disables the provider
    import pyotp
    import robin_stocks.robinhood as rh
except ImportError:  # pragma: no cover - exercised only without the extra
    pyotp = None
    rh = None


class OrderRefused(RuntimeError):
    """A write was refused by a guardrail (no confirm, no stop, not configured).

    Its own type so callers distinguish a *refusal* (never sent) from a broker
    rejection (sent and declined).
    """


class RobinhoodProvider:
    """Read the account and place a confirmed long-with-stop on Robinhood."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._logged_in = False

    # -- configuration / guardrails ------------------------------------------

    @property
    def available(self) -> bool:
        """True only when the library is present and username+password are set.

        MFA (`rh_mfa_totp`) is optional here but effectively required by any
        real Robinhood account with 2FA on; login simply fails without it.
        """
        return bool(
            rh is not None
            and getattr(self.settings, "rh_username", None)
            and getattr(self.settings, "rh_password", None)
        )

    def _mfa_code(self) -> str | None:
        secret = getattr(self.settings, "rh_mfa_totp", None)
        if not secret or pyotp is None:
            return None
        return pyotp.TOTP(secret).now()

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        if not self.available:
            raise OrderRefused(
                "Robinhood no está configurado (faltan RH_USERNAME/RH_PASSWORD "
                "en API/.env)."
            )
        # Plan B: reuse the stored session. Never authenticate interactively from
        # here — if there is no session, refuse and tell Angel to run the one-time
        # login, rather than block on robin_stocks' MFA input() prompt.
        if not self._has_totp and not SESSION_FILE.exists():
            raise OrderRefused(
                "No hay sesión de Robinhood. Corre una vez en la terminal: "
                ".venv/bin/python scripts/rh_login.py  (inicia sesión, teclea el "
                "código MFA cuando lo pida, y guarda la sesión)."
            )
        rh.login(
            username=self.settings.rh_username,
            password=self.settings.rh_password,
            mfa_code=self._mfa_code(),
            store_session=True,
        )
        self._logged_in = True

    @property
    def _has_totp(self) -> bool:
        return bool(getattr(self.settings, "rh_mfa_totp", None))

    # -- reads ----------------------------------------------------------------

    def buying_power(self) -> float | None:
        """Cash buying power, or None if unavailable/unparsable (reads never raise)."""
        if not self.available:
            return None
        try:
            self._ensure_login()
            val = rh.profiles.load_account_profile(info="buying_power")
            return float(val) if val is not None else None
        except (ValueError, TypeError, KeyError, OrderRefused) as exc:
            logger.warning("robinhood buying_power failed: %s", exc)
            return None

    def positions(self) -> dict | None:
        """Current holdings keyed by symbol, or None on failure."""
        if not self.available:
            return None
        try:
            self._ensure_login()
            return rh.build_holdings()
        except Exception as exc:  # noqa: BLE001 - reads never raise
            logger.warning("robinhood positions failed: %s", exc)
            return None

    # -- write (confirmed, stop-attached, long-only) --------------------------

    def place_long_with_stop(
        self,
        symbol: str,
        qty: int,
        entry_limit: float,
        stop_price: float,
        *,
        confirm: bool,
        time_in_force: str = "gtc",
    ) -> dict:
        """Place a limit BUY, then a protective stop-loss SELL. Real money.

        Fails closed: raises `OrderRefused` unless `confirm is True`, unless a
        positive stop below the entry is given, and unless qty >= 1. Returns
        `{"status", "entry", "stop", "note"}`; `status` is "placed" when both
        legs were accepted, or "unhedged" when the entry was accepted but the
        stop leg was rejected (the caller must surface that prominently).
        """
        if confirm is not True:
            raise OrderRefused("Orden no confirmada: place_long_with_stop exige confirm=True.")
        symbol = (symbol or "").upper().strip()
        if not symbol:
            raise OrderRefused("Falta el símbolo.")
        if qty is None or qty < 1:
            raise OrderRefused(f"Cantidad inválida {qty!r}: debe ser >= 1.")
        if entry_limit is None or entry_limit <= 0:
            raise OrderRefused("Precio de entrada inválido.")
        if stop_price is None or stop_price <= 0:
            raise OrderRefused("Sin stop: sin stop no se abre la posición.")
        if stop_price >= entry_limit:
            raise OrderRefused("El stop debe estar por debajo de la entrada (posición larga).")

        self._ensure_login()

        entry = rh.order_buy_limit(
            symbol, qty, entry_limit, timeInForce=time_in_force
        )
        if not _accepted(entry):
            return {"status": "rejected", "entry": entry, "stop": None,
                    "note": "Robinhood rechazó la orden de entrada; no se colocó stop."}

        try:
            stop = rh.order_sell_stop_loss(
                symbol, qty, stop_price, timeInForce=time_in_force
            )
        except Exception as exc:  # noqa: BLE001 - report, don't crash mid-position
            logger.error("robinhood stop leg raised: %s", exc)
            stop = None

        if not _accepted(stop):
            return {
                "status": "unhedged", "entry": entry, "stop": stop,
                "note": ("La entrada se envió pero el STOP fue rechazado (común si la "
                         "limit aún no llena: Robinhood no deja poner stop sobre acciones "
                         "que todavía no posees). Coloca el stop manualmente al llenarse, "
                         "o usa entrada de mercado para que el stop pegue de inmediato."),
            }

        return {"status": "placed", "entry": entry, "stop": stop, "note": None}


def _accepted(order: Any) -> bool:
    """A robin_stocks order dict is accepted when it carries an id and no error."""
    if not isinstance(order, dict):
        return False
    if order.get("detail") and not order.get("id"):
        return False
    return bool(order.get("id"))

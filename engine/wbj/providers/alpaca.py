"""Alpaca **paper** trading provider — read the account, place simulated orders.

This module is deliberately paper-only, and enforces it three ways so that no
configuration mistake can route an order to a real account:

1. There is only one base URL constant, the paper host. The live endpoint
   (`api.alpaca.markets`) is not named anywhere in this file, so there is nothing
   to accidentally point at.
2. `_assert_paper()` runs at the top of every request. It refuses unless the key
   id has Alpaca's paper prefix `PK` (live keys start with `AK`) and
   `settings.alpaca_paper` is True. It raises before any network call.
3. Order placement (`place_paper_order`) calls that same guard, so a live key can
   never reach the order endpoint even though the endpoint itself is paper-only.

The project rule stands above this file: real-money execution is always manual
(`CLAUDE.md`). Paper orders move nothing real — they exist to forward-test how the
agent's research classifications would have played out.
"""

from __future__ import annotations

from typing import Any

import httpx

# El ÚNICO host. El de real (api.alpaca.markets) no se nombra aquí a propósito:
# lo que no existe en el código no se puede apuntar por error.
PAPER_BASE = "https://paper-api.alpaca.markets"

_PAPER_KEY_PREFIX = "PK"   # las llaves de paper empiezan con PK; las de real con AK
_VALID_SIDES = frozenset({"buy", "sell"})
_VALID_TYPES = frozenset({"market", "limit"})
_VALID_TIF = frozenset({"day", "gtc", "ioc", "fok", "opg", "cls"})


class NotPaperError(RuntimeError):
    """Raised when something would run against a non-paper key or live mode.

    Its own class so a caller can catch exactly this — a refusal to touch a real
    account is not the same failure as a network timeout.
    """


class AlpacaPaperProvider:
    """Read-and-simulate client for an Alpaca **paper** account.

    `available` is True only when both credentials are present, paper mode is on,
    and the key id is a paper key. When it is False every method returns None (reads)
    or raises `NotPaperError` (writes) rather than reaching the network.
    """

    def __init__(self, settings: Any, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client if client is not None else httpx.Client(timeout=10.0)

    # -- guardarraíles --------------------------------------------------------

    @property
    def _key_id(self) -> str | None:
        return getattr(self.settings, "alpaca_key_id", None)

    @property
    def _secret(self) -> str | None:
        return getattr(self.settings, "alpaca_secret", None)

    @property
    def _is_paper_key(self) -> bool:
        kid = self._key_id or ""
        return kid.startswith(_PAPER_KEY_PREFIX)

    @property
    def available(self) -> bool:
        """True iff paper credentials are present, paper mode is on, and the key is
        a paper key. Anything else disables the provider — it never guesses."""
        return bool(
            self._key_id
            and self._secret
            and getattr(self.settings, "alpaca_paper", False)
            and self._is_paper_key
        )

    def _assert_paper(self) -> None:
        """Refuse, loudly and before any network call, anything that isn't paper.

        This is the last line between "test harness" and "real account". It runs on
        reads and writes alike.
        """
        if not getattr(self.settings, "alpaca_paper", False):
            raise NotPaperError(
                "ALPACA_PAPER está en false. Este proveedor es solo para paper "
                "trading; la ejecución con dinero real la haces tú, a mano."
            )
        if not self._is_paper_key:
            raise NotPaperError(
                "La llave no es de paper (debe empezar con 'PK'; las de real empiezan "
                "con 'AK'). Me niego a operar una cuenta real."
            )
        if not self._key_id or not self._secret:
            raise NotPaperError("Faltan credenciales de Alpaca (key id y/o secret).")

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key_id or "",
            "APCA-API-SECRET-KEY": self._secret or "",
        }

    def _get(self, path: str, params: dict | None = None) -> Any | None:
        """GET against the paper host. Returns None on any failure — reads never
        raise, mirroring the other providers' contract."""
        if not self.available:
            return None
        self._assert_paper()
        url = f"{PAPER_BASE}{path}"
        try:
            r = self.client.get(url, params=params or {}, headers=self._headers())
        except httpx.HTTPError:
            return None
        if r.status_code >= 400:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # -- lecturas -------------------------------------------------------------

    def account(self) -> dict | None:
        """Paper account: equity, buying power, cash, status."""
        return self._get("/v2/account")

    def positions(self) -> list | None:
        """Open paper positions: symbol, qty, avg entry, market value, unrealized P/L."""
        return self._get("/v2/positions")

    def orders(self, status: str = "all", limit: int = 100) -> list | None:
        """Paper order history. `status` is one of open/closed/all."""
        return self._get("/v2/orders", {"status": status, "limit": limit})

    # -- escritura (solo paper) ----------------------------------------------

    def place_paper_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: float | None = None,
    ) -> dict:
        """Place a **simulated** order on the paper account.

        Validates its own arguments and re-checks the paper guard before sending, so
        a bad side/type never leaves the process and a live key never reaches the
        endpoint. Unlike the reads, this raises on failure: a forward-test must know
        whether its order was actually accepted, never silently assume it was.
        """
        self._assert_paper()

        side = side.lower().strip()
        order_type = order_type.lower().strip()
        time_in_force = time_in_force.lower().strip()
        if side not in _VALID_SIDES:
            raise ValueError(f"side inválido {side!r}; usa buy o sell.")
        if order_type not in _VALID_TYPES:
            raise ValueError(f"order_type inválido {order_type!r}; usa market o limit.")
        if time_in_force not in _VALID_TIF:
            raise ValueError(f"time_in_force inválido {time_in_force!r}.")
        if qty <= 0:
            raise ValueError(f"qty debe ser positiva, no {qty!r}.")
        if order_type == "limit" and limit_price is None:
            raise ValueError("una orden limit necesita limit_price.")

        body: dict[str, Any] = {
            "symbol": symbol.upper().strip(),
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            body["limit_price"] = str(limit_price)

        r = self.client.post(
            f"{PAPER_BASE}/v2/orders", json=body, headers=self._headers()
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Alpaca rechazó la orden paper ({r.status_code}): {r.text}"
            )
        return r.json()

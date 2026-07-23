"""Order-ticket builder: turn a research level + risk rule into a sized order.

Pure functions, no network. The web app shows the ticket this produces; the
user reviews it and clicks Enviar; only then does `wbj.providers.robinhood`
place it. Nothing here talks to a broker.

Sizing follows Angel's profile rule (`Perfil Inversionista/Angel Negron.md`):
risk 3-5% of capital per trade, and that risk is the loss if the stop is hit,
never the notional deployed:

    stop_distance = |entry - stop|
    risk_dollars  = equity * risk_frac
    qty           = floor(risk_dollars / stop_distance)

So a filled stop costs about `risk_frac` of equity. Without a stop the risk is
undefined, so a ticket without a valid stop is NOT_TRADEABLE — never guessed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

# Profile risk band (fraction of capital risked per trade). The report defaults
# to the conservative edge; the UI may pass 0.03-0.05.
RISK_FRAC_DEFAULT = 0.03
RISK_FRAC_MIN = 0.03
RISK_FRAC_MAX = 0.05

_VALID_SIDES = frozenset({"buy", "sell"})


@dataclass(frozen=True)
class OrderTicket:
    """A fully-specified, human-reviewable order. `tradeable` gates the button."""

    ticker: str
    side: str
    entry: float
    stop: float
    qty: int
    risk_frac: float
    risk_dollars: float
    stop_distance: float
    notional: float
    tradeable: bool
    reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _not_tradeable(ticker, side, entry, stop, risk_frac, reason) -> OrderTicket:
    return OrderTicket(
        ticker=ticker, side=side, entry=float(entry or 0), stop=float(stop or 0),
        qty=0, risk_frac=risk_frac, risk_dollars=0.0, stop_distance=0.0,
        notional=0.0, tradeable=False, reason=reason,
    )


def build_ticket(
    ticker: str,
    side: str,
    entry: float | None,
    stop: float | None,
    equity: float | None,
    risk_frac: float = RISK_FRAC_DEFAULT,
    max_notional: float | None = None,
) -> OrderTicket:
    """Build a risk-sized order ticket, or a NOT_TRADEABLE one with a reason.

    `side` is "buy" (stop must sit *below* entry) or "sell" (stop *above*).
    `equity` sizes the risk budget; `max_notional` (e.g. buying power) caps the
    quantity so a cash account never over-commits. Every failure path returns a
    ticket with `tradeable=False` and a human reason rather than raising — the
    UI renders the reason instead of an order button.
    """
    ticker = (ticker or "").upper().strip()
    side = (side or "").lower().strip()
    if side not in _VALID_SIDES:
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              f"Lado inválido {side!r}: usa buy o sell.")
    if not ticker:
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              "Falta el ticker.")
    if entry is None or entry <= 0:
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              "Sin precio de entrada válido.")
    if stop is None or stop <= 0:
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              "Sin stop: sin stop no se abre la posición.")

    # Stop must be on the protective side of the entry.
    if side == "buy" and stop >= entry:
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              "En compra el stop debe estar por debajo de la entrada.")
    if side == "sell" and stop <= entry:
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              "En venta el stop debe estar por encima de la entrada.")

    if not (RISK_FRAC_MIN <= risk_frac <= RISK_FRAC_MAX):
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              f"Riesgo {risk_frac:.0%} fuera de la banda 3–5% del perfil.")
    if equity is None or equity <= 0:
        return _not_tradeable(ticker, side, entry, stop, risk_frac,
                              "Sin capital de la cuenta para dimensionar.")

    stop_distance = abs(entry - stop)
    risk_dollars = equity * risk_frac
    qty = math.floor(risk_dollars / stop_distance)

    # Cap so notional never exceeds available buying power (cash account).
    capped = False
    cap_notional = max_notional if max_notional is not None else equity
    if cap_notional is not None and cap_notional > 0:
        max_qty = math.floor(cap_notional / entry)
        if qty > max_qty:
            qty, capped = max_qty, True

    if qty < 1:
        return _not_tradeable(
            ticker, side, entry, stop, risk_frac,
            "Tamaño < 1 acción: el stop es muy ancho para el 3–5% de riesgo "
            "(o el capital es muy chico). No se abre.",
        )

    reason = None
    if capped:
        reason = ("Cantidad limitada por el poder de compra disponible; el riesgo "
                  "real queda por debajo del 3–5% objetivo.")

    return OrderTicket(
        ticker=ticker, side=side, entry=float(entry), stop=float(stop), qty=int(qty),
        risk_frac=risk_frac, risk_dollars=round(qty * stop_distance, 2),
        stop_distance=round(stop_distance, 4), notional=round(qty * entry, 2),
        tradeable=True, reason=reason,
    )

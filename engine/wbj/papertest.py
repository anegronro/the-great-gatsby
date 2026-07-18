"""Paper forward-test harness: turn a research classification into a simulated trade.

This is the piece Angel asked for — read the classification, size at 2% risk, go
long on favorable / short on Avoid, fire automatically on the **paper** account. It
exists to answer one question over time: if the agent had traded its own
classifications, how would it have done?

It sits on the aggregation pipeline (`wbj.aggregate.pipeline.classify`): the input
is that pipeline's `ProfileResult.label` — the same gate string a report shows
("Momentum Candidate", "Avoid / Wait", ...) — never a re-derived signal.

Boundaries this file keeps:

- **Paper only.** It never constructs a provider; the caller passes an
  `AlpacaPaperProvider`, which is paper-only by construction. Real-money execution
  stays manual (`CLAUDE.md`), always.
- **It declines more than it trades, on purpose.** A name only trades when it
  reaches a directional gate; Conditional / Speculative / Weak are honest skips, not
  idle ones. And a name with thin qualitative coverage lands on the coverage
  override (Conditional), so the harness stays quiet until the judgment overlay
  fills Business/Market/Risk — the system being careful, not broken.
- **Every decision is logged**, traded or skipped, so the forward test is auditable
  — the same standard the reports hold themselves to.

"Risk 2%" means the loss if the stop is hit, not the notional deployed:

    stop_distance = atr_mult * ATR14
    qty           = floor(equity * risk / stop_distance)

so a filled stop costs about `risk` of equity. Without an ATR there is no stop, and
without a stop the 2% is undefined — so the harness skips rather than guess.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wbj.aggregate.gates import (
    GATE_AVOID,
    GATE_MOMENTUM,
    GATE_QUALITY,
    GATE_VALUE,
)

# Gates favorables -> largo; Avoid -> corto; el resto (Conditional, Speculative,
# Weak) no se opera. Son los strings de `ProfileResult.label`, una sola fuente.
_LONG_LABELS = frozenset({GATE_MOMENTUM, GATE_QUALITY, GATE_VALUE})
_SHORT_LABELS = frozenset({GATE_AVOID})

_DEFAULT_RISK = 0.02      # 2% del equity en riesgo si salta el stop
_DEFAULT_ATR_MULT = 2.0   # stop a 2*ATR14 del precio de entrada


@dataclass
class Decision:
    """A sizing decision: long, short, or skip — always with a reason."""

    action: str            # "long" | "short" | "skip"
    reason: str
    symbol: str = ""
    qty: int = 0
    entry: float | None = None
    stop: float | None = None
    stop_distance: float | None = None
    risk_amount: float | None = None


def decide(
    label: str,
    price: float | None,
    atr: float | None,
    equity: float,
    risk: float = _DEFAULT_RISK,
    atr_mult: float = _DEFAULT_ATR_MULT,
    symbol: str = "",
) -> Decision:
    """Pure sizing decision from a profile label, price, ATR and equity.

    `label` is a `ProfileResult.label` gate string. Returns a `Decision` in every
    case: a skip carries the reason so the log can say *why* nothing was traded,
    which for a forward test matters as much as the fills.
    """
    if label in _LONG_LABELS:
        side = "long"
    elif label in _SHORT_LABELS:
        side = "short"
    else:
        return Decision("skip", f"Perfil '{label}' no es una señal clara de largo ni "
                        "corto; no se opera.", symbol=symbol)

    if price is None or price <= 0:
        return Decision("skip", "Sin precio de mercado utilizable.", symbol=symbol)
    if atr is None or atr <= 0:
        return Decision("skip", "Sin ATR: no hay stop, y sin stop el 2% no está "
                        "definido.", symbol=symbol)
    if equity <= 0:
        return Decision("skip", "Equity no positivo en la cuenta.", symbol=symbol)

    stop_distance = atr_mult * atr
    risk_amount = equity * risk
    qty = int(risk_amount // stop_distance)   # truncado: redondear arriba pasaría del 2%
    if qty < 1:
        return Decision("skip", f"El presupuesto de riesgo (${risk_amount:,.0f}) frente "
                        f"a un stop de ${stop_distance:,.2f} da menos de 1 acción.",
                        symbol=symbol)

    stop = price - stop_distance if side == "long" else price + stop_distance
    return Decision(
        action=side, reason=f"Perfil '{label}' -> {side}.", symbol=symbol,
        qty=qty, entry=round(price, 4), stop=round(stop, 4),
        stop_distance=round(stop_distance, 4), risk_amount=round(risk_amount, 2),
    )


def _label_of(classification: Any) -> str:
    """The gate label off a `Classification` (`.profile.label`), tolerating a
    bare `ProfileResult` or a raw string so callers/tests can pass either."""
    prof = getattr(classification, "profile", classification)
    return getattr(prof, "label", prof) if not isinstance(prof, str) else prof


def _held_symbols(provider: Any) -> set[str]:
    positions = provider.positions() or []
    return {p.get("symbol", "").upper() for p in positions}


def _equity(provider: Any) -> float | None:
    acct = provider.account()
    if not acct:
        return None
    try:
        return float(acct.get("equity"))
    except (TypeError, ValueError):
        return None


def run_papertest(
    ticker: str,
    provider: Any,
    classification: Any,
    price: float | None,
    atr: float | None,
    risk: float = _DEFAULT_RISK,
    atr_mult: float = _DEFAULT_ATR_MULT,
    log_dir: Path | None = None,
) -> dict:
    """Classify -> size -> fire (paper) -> log, for one ticker.

    `classification` is a `wbj.aggregate.pipeline.Classification` (its
    `.profile.label` is the gate). `price` and `atr` come from the technical
    engine's inputs. Returns the outcome dict and, if `log_dir` is given, appends
    it to a JSON-lines decision log.

    Outcomes: long / short (order placed), skip (nothing to do, with reason), or
    rejected (the broker refused — e.g. a non-shortable symbol — recorded, not raised).
    """
    ticker = ticker.upper()
    label = _label_of(classification)
    outcome: dict = {"ticker": ticker, "ts": datetime.now(timezone.utc).isoformat(),
                     "profile": label}

    if not getattr(provider, "available", False):
        outcome.update(action="skip", reason="Proveedor Alpaca no disponible "
                       "(credenciales o modo paper).")
        return _log(outcome, log_dir)

    if ticker in _held_symbols(provider):
        outcome.update(action="skip", reason="Ya hay posición abierta en el símbolo; "
                       "el auto-fire no apila.")
        return _log(outcome, log_dir)

    equity = _equity(provider)
    if equity is None:
        outcome.update(action="skip", reason="No se pudo leer el equity de la cuenta.")
        return _log(outcome, log_dir)

    d = decide(label, price, atr, equity, risk, atr_mult, ticker)
    outcome.update(action=d.action, reason=d.reason, **{
        k: v for k, v in asdict(d).items()
        if k in ("qty", "entry", "stop", "stop_distance", "risk_amount")
    })
    if d.action == "skip":
        return _log(outcome, log_dir)

    side = "buy" if d.action == "long" else "sell"
    try:
        order = provider.place_paper_order(ticker, d.qty, side)
        outcome["order_id"] = order.get("id")
        outcome["order_status"] = order.get("status")
    except RuntimeError as e:
        outcome["action"] = "rejected"
        outcome["reason"] = f"{d.reason} Broker rechazó: {e}"
    return _log(outcome, log_dir)


def _log(outcome: dict, log_dir: Path | None) -> dict:
    """Append the outcome to a JSON-lines log so the forward test is auditable."""
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "papertest.jsonl").open("a") as fh:
            fh.write(json.dumps(outcome, ensure_ascii=False) + "\n")
    return outcome

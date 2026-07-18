"""Tests for the paper forward-test harness — `wbj.papertest`.

The decision logic (`decide`) is a pure function of the classification label, price,
ATR and equity, so it is tested here directly and exhaustively. The point is the
mapping and the sizing math: what becomes a long, a short, or a skip, and how "risk
2%" turns into a share count.

Nothing here touches Alpaca. The orchestration (`run_papertest`) is tested against a
fake provider in the second half.
"""

import pytest

from wbj.aggregate.gates import (
    GATE_AVOID,
    GATE_CONDITIONAL,
    GATE_MOMENTUM,
    GATE_QUALITY,
    GATE_SPECULATIVE,
    GATE_VALUE,
    GATE_WEAK,
)
from wbj.papertest import Decision, decide, run_papertest


# -- el mapeo: qué perfil se opera y en qué dirección ------------------------


@pytest.mark.parametrize("label", [GATE_MOMENTUM, GATE_QUALITY, GATE_VALUE])
def test_favorable_profiles_go_long(label):
    d = decide(label, price=100.0, atr=5.0, equity=100_000, risk=0.02)
    assert d.action == "long"


def test_avoid_goes_short():
    d = decide(GATE_AVOID, price=100.0, atr=5.0, equity=100_000, risk=0.02)
    assert d.action == "short"


@pytest.mark.parametrize("label", [GATE_CONDITIONAL, GATE_SPECULATIVE, GATE_WEAK])
def test_unclear_profiles_are_skipped_with_a_reason(label):
    """'No lo tengo claro' no se opera. La mayoría cae aquí con cobertura fina."""
    d = decide(label, price=100.0, atr=5.0, equity=100_000, risk=0.02)
    assert d.action == "skip"
    assert d.reason


# -- el tamaño: "riesgo 2%" = pérdida si salta el stop -----------------------


def test_size_risks_exactly_the_target_fraction_of_equity():
    """equity 100k, riesgo 2% -> $2000 en riesgo. Stop a 2*ATR = 2*5 = $10.
    qty = 2000 / 10 = 200 acciones."""
    d = decide(GATE_MOMENTUM, price=100.0, atr=5.0, equity=100_000,
               risk=0.02, atr_mult=2.0)
    assert d.qty == 200
    assert d.stop_distance == pytest.approx(10.0)
    # la pérdida en el stop es ~2% del equity, que es la definición del 2%
    assert d.qty * d.stop_distance == pytest.approx(2000.0)


def test_long_stop_is_below_entry_short_stop_is_above():
    lo = decide(GATE_MOMENTUM, price=100.0, atr=5.0, equity=100_000)
    assert lo.stop < lo.entry
    sh = decide(GATE_AVOID, price=100.0, atr=5.0, equity=100_000)
    assert sh.stop > sh.entry


def test_qty_is_floored_never_rounded_up():
    """Redondear hacia arriba arriesgaría más del 2%. Se trunca."""
    # 2000 / 7 = 285.7 -> 285
    d = decide(GATE_MOMENTUM, price=100.0, atr=3.5, equity=100_000,
               risk=0.02, atr_mult=2.0)
    assert d.qty == 285


def test_wider_risk_budget_buys_more():
    small = decide(GATE_MOMENTUM, price=100.0, atr=5.0, equity=100_000, risk=0.02)
    big = decide(GATE_MOMENTUM, price=100.0, atr=5.0, equity=100_000, risk=0.05)
    assert big.qty > small.qty


# -- guardas ------------------------------------------------------------------


def test_skips_when_the_position_would_be_under_one_share():
    """Un ATR enorme frente a un presupuesto chico da <1 acción: no se opera."""
    d = decide(GATE_MOMENTUM, price=100.0, atr=5000.0, equity=100_000, risk=0.02)
    assert d.action == "skip"
    assert "acción" in d.reason or "1" in d.reason


def test_skips_without_a_usable_atr():
    """Sin ATR no hay stop, y sin stop el 2% no está definido."""
    d = decide(GATE_MOMENTUM, price=100.0, atr=None, equity=100_000)
    assert d.action == "skip"


def test_skips_without_a_price():
    d = decide(GATE_MOMENTUM, price=None, atr=5.0, equity=100_000)
    assert d.action == "skip"


def test_skips_on_nonpositive_equity():
    d = decide(GATE_MOMENTUM, price=100.0, atr=5.0, equity=0)
    assert d.action == "skip"


# -- orquestación contra un proveedor falso ----------------------------------


class _FakeProvider:
    """Stand-in for AlpacaPaperProvider: records orders, never hits the network."""

    def __init__(self, equity=100_000, positions=None, available=True,
                 shortable=True):
        self.available = available
        self._equity = equity
        self._positions = positions or []
        self._shortable = shortable
        self.orders = []

    def account(self):
        return {"equity": str(self._equity), "buying_power": str(self._equity * 4),
                "status": "ACTIVE"}

    def positions(self):
        return self._positions

    def place_paper_order(self, symbol, qty, side, order_type="market",
                          time_in_force="day", limit_price=None):
        if side == "sell" and not self._shortable:
            raise RuntimeError("Alpaca rechazó la orden paper (422): not shortable")
        order = {"symbol": symbol, "qty": str(qty), "side": side, "status": "accepted"}
        self.orders.append(order)
        return order


def _classification(label: str):
    """A minimal object shaped like pipeline.Classification for the harness:
    `.profile.label` carries the gate string."""
    class _Profile:
        pass

    class _C:
        pass

    prof = _Profile()
    prof.label = label
    c = _C()
    c.profile = prof
    return c


def test_run_places_a_long_order_for_a_favorable_classification():
    prov = _FakeProvider()
    res = run_papertest("NVDA", prov,
                        classification=_classification(GATE_MOMENTUM),
                        price=100.0, atr=5.0, risk=0.02)
    assert res["action"] == "long"
    assert res["profile"] == GATE_MOMENTUM
    assert len(prov.orders) == 1
    assert prov.orders[0]["side"] == "buy"
    assert prov.orders[0]["qty"] == "200"


def test_run_places_a_short_order_for_avoid():
    prov = _FakeProvider()
    res = run_papertest("XYZ", prov, classification=_classification(GATE_AVOID),
                        price=100.0, atr=5.0, risk=0.02)
    assert res["action"] == "short"
    assert prov.orders[0]["side"] == "sell"


def test_run_does_not_order_on_a_skip():
    prov = _FakeProvider()
    res = run_papertest("AAPL", prov,
                        classification=_classification(GATE_CONDITIONAL),
                        price=100.0, atr=5.0)
    assert res["action"] == "skip"
    assert prov.orders == []


def test_run_skips_a_symbol_already_held():
    """Auto-fire no debe apilar: si ya hay posición en el símbolo, no re-entra."""
    prov = _FakeProvider(positions=[{"symbol": "NVDA", "qty": "10"}])
    res = run_papertest("NVDA", prov,
                        classification=_classification(GATE_MOMENTUM),
                        price=100.0, atr=5.0)
    assert res["action"] == "skip"
    assert "posición" in res["reason"]
    assert prov.orders == []


def test_run_reports_a_rejected_short_without_crashing():
    """Si el activo no es shorteable, Alpaca rechaza: se registra, no se revienta."""
    prov = _FakeProvider(shortable=False)
    res = run_papertest("XYZ", prov, classification=_classification(GATE_AVOID),
                        price=100.0, atr=5.0)
    assert res["action"] == "rejected"
    assert "shortable" in res["reason"].lower() or "422" in res["reason"]


def test_run_refuses_when_provider_unavailable():
    prov = _FakeProvider(available=False)
    res = run_papertest("NVDA", prov,
                        classification=_classification(GATE_MOMENTUM),
                        price=100.0, atr=5.0)
    assert res["action"] == "skip"
    assert prov.orders == []

"""Tests for wbj.order.build_ticket — risk-sized order ticket, no network."""

from wbj.order import RISK_FRAC_DEFAULT, build_ticket


def test_buy_ticket_sizes_qty_to_risk_over_stop_distance():
    # equity 1000, risk 3% = $30; stop distance $2 -> 15 shares.
    t = build_ticket("NVDA", "buy", entry=100.0, stop=98.0, equity=1000.0,
                     risk_frac=0.03, max_notional=10_000)
    assert t.tradeable is True
    assert t.qty == 15
    assert t.stop_distance == 2.0
    assert t.risk_dollars == 30.0
    assert t.notional == 1500.0


def test_buy_ticket_caps_qty_at_buying_power():
    # Risk would allow many shares, but only $500 buying power at $100 -> 5 shares.
    t = build_ticket("NVDA", "buy", entry=100.0, stop=99.0, equity=100_000.0,
                     risk_frac=0.03, max_notional=500.0)
    assert t.tradeable is True
    assert t.qty == 5
    assert t.notional == 500.0
    assert "poder de compra" in (t.reason or "")


def test_no_stop_is_not_tradeable():
    t = build_ticket("NVDA", "buy", entry=100.0, stop=None, equity=1000.0)
    assert t.tradeable is False
    assert "stop" in t.reason.lower()


def test_buy_stop_must_be_below_entry():
    t = build_ticket("NVDA", "buy", entry=100.0, stop=101.0, equity=1000.0)
    assert t.tradeable is False
    assert "por debajo" in t.reason


def test_sell_stop_must_be_above_entry():
    t = build_ticket("NVDA", "sell", entry=100.0, stop=99.0, equity=1000.0)
    assert t.tradeable is False
    assert "por encima" in t.reason


def test_qty_below_one_is_not_tradeable():
    # $30 risk / $50 stop distance -> 0 shares.
    t = build_ticket("NVDA", "buy", entry=100.0, stop=50.0, equity=1000.0,
                     risk_frac=0.03)
    assert t.tradeable is False
    assert "< 1" in t.reason


def test_risk_frac_outside_band_is_rejected():
    t = build_ticket("NVDA", "buy", entry=100.0, stop=98.0, equity=1000.0,
                     risk_frac=0.10)
    assert t.tradeable is False
    assert "3–5%" in t.reason


def test_invalid_side_is_rejected():
    t = build_ticket("NVDA", "hold", entry=100.0, stop=98.0, equity=1000.0)
    assert t.tradeable is False
    assert "Lado inválido" in t.reason


def test_default_risk_frac_is_conservative_edge():
    assert RISK_FRAC_DEFAULT == 0.03

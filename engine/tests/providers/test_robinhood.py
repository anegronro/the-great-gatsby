"""Tests for wbj.providers.robinhood — guardrails first, no real orders.

Every network call is monkeypatched. These tests never touch Robinhood; they
prove the provider fails closed (no confirm / no stop => refused before any
call) and reports an unhedged entry loudly.
"""

import types

import pytest

import wbj.providers.robinhood as rhmod
from wbj.providers.robinhood import OrderRefused, RobinhoodProvider


class _Settings:
    def __init__(self, user="u", pw="p", totp=None):
        self.rh_username = user
        self.rh_password = pw
        self.rh_mfa_totp = totp


def _fake_rh(monkeypatch, tmp_path=None, *, entry_ok=True, stop_ok=True,
            session=True, calls=None):
    """Install a fake robin_stocks.robinhood module and record order calls.

    `session` controls whether a stored-session file appears to exist (Plan B):
    when True, SESSION_FILE is pointed at a real temp file so `_ensure_login`
    proceeds; when False it points at a missing path so login refuses.
    """
    calls = calls if calls is not None else {}

    if tmp_path is not None or session is False:
        sess = (tmp_path / "robinhood.pickle") if tmp_path else \
            __import__("pathlib").Path("/nonexistent/robinhood.pickle")
        if session and tmp_path is not None:
            sess.write_bytes(b"x")
        monkeypatch.setattr(rhmod, "SESSION_FILE", sess)

    def login(**kwargs):
        calls["login"] = kwargs
        return {"access_token": "x"}

    def order_buy_limit(symbol, quantity, limitPrice, timeInForce="gtc"):
        calls["buy"] = (symbol, quantity, limitPrice, timeInForce)
        return {"id": "entry-1"} if entry_ok else {"detail": "rejected"}

    def order_sell_stop_loss(symbol, quantity, stopPrice, timeInForce="gtc"):
        calls["stop"] = (symbol, quantity, stopPrice, timeInForce)
        return {"id": "stop-1"} if stop_ok else {"detail": "not enough shares"}

    fake = types.SimpleNamespace(
        login=login,
        order_buy_limit=order_buy_limit,
        order_sell_stop_loss=order_sell_stop_loss,
        profiles=types.SimpleNamespace(
            load_account_profile=lambda info=None: "1234.56"
        ),
        build_holdings=lambda: {"NVDA": {"quantity": "10"}},
    )
    monkeypatch.setattr(rhmod, "rh", fake)
    return calls


# --- guardrails: fail closed -------------------------------------------------


def test_place_without_confirm_refuses_before_any_call(monkeypatch):
    calls = _fake_rh(monkeypatch)
    p = RobinhoodProvider(_Settings())
    with pytest.raises(OrderRefused, match="confirm"):
        p.place_long_with_stop("NVDA", 10, 100.0, 98.0, confirm=False)
    assert "buy" not in calls and "login" not in calls  # nothing sent


def test_place_without_stop_refuses(monkeypatch):
    _fake_rh(monkeypatch)
    p = RobinhoodProvider(_Settings())
    with pytest.raises(OrderRefused, match="[Ss]top"):
        p.place_long_with_stop("NVDA", 10, 100.0, 0.0, confirm=True)


def test_place_with_stop_at_or_above_entry_refuses(monkeypatch):
    _fake_rh(monkeypatch)
    p = RobinhoodProvider(_Settings())
    with pytest.raises(OrderRefused, match="debajo"):
        p.place_long_with_stop("NVDA", 10, 100.0, 100.0, confirm=True)


def test_place_with_bad_qty_refuses(monkeypatch):
    _fake_rh(monkeypatch)
    p = RobinhoodProvider(_Settings())
    with pytest.raises(OrderRefused, match="Cantidad"):
        p.place_long_with_stop("NVDA", 0, 100.0, 98.0, confirm=True)


def test_unconfigured_provider_refuses_at_login(monkeypatch):
    _fake_rh(monkeypatch)
    p = RobinhoodProvider(_Settings(user=None))
    with pytest.raises(OrderRefused, match="no está configurado"):
        p.place_long_with_stop("NVDA", 10, 100.0, 98.0, confirm=True)


# --- happy path & unhedged reporting ----------------------------------------


def test_confirmed_order_places_both_legs(monkeypatch, tmp_path):
    calls = _fake_rh(monkeypatch, tmp_path)
    p = RobinhoodProvider(_Settings())
    res = p.place_long_with_stop("nvda", 10, 100.0, 98.0, confirm=True)
    assert res["status"] == "placed"
    assert calls["buy"] == ("NVDA", 10, 100.0, "gtc")
    assert calls["stop"] == ("NVDA", 10, 98.0, "gtc")


def test_place_refuses_without_stored_session(monkeypatch):
    # Plan B: no session file -> refuse with guidance, never prompt/hang.
    _fake_rh(monkeypatch, session=False)
    p = RobinhoodProvider(_Settings())
    with pytest.raises(OrderRefused, match="rh_login"):
        p.place_long_with_stop("NVDA", 10, 100.0, 98.0, confirm=True)


def test_stop_rejection_reports_unhedged(monkeypatch, tmp_path):
    _fake_rh(monkeypatch, tmp_path, stop_ok=False)
    p = RobinhoodProvider(_Settings())
    res = p.place_long_with_stop("NVDA", 10, 100.0, 98.0, confirm=True)
    assert res["status"] == "unhedged"
    assert "STOP" in res["note"]


def test_entry_rejection_places_no_stop(monkeypatch, tmp_path):
    calls = _fake_rh(monkeypatch, tmp_path, entry_ok=False)
    p = RobinhoodProvider(_Settings())
    res = p.place_long_with_stop("NVDA", 10, 100.0, 98.0, confirm=True)
    assert res["status"] == "rejected"
    assert "stop" not in calls  # never attempted the protective leg


# --- reads -------------------------------------------------------------------


def test_available_false_without_credentials(monkeypatch):
    _fake_rh(monkeypatch)
    assert RobinhoodProvider(_Settings(user=None)).available is False


def test_buying_power_parses_float(monkeypatch, tmp_path):
    _fake_rh(monkeypatch, tmp_path)
    assert RobinhoodProvider(_Settings()).buying_power() == 1234.56


def test_mfa_code_generated_from_totp(monkeypatch):
    _fake_rh(monkeypatch)
    p = RobinhoodProvider(_Settings(totp="JBSWY3DPEHPK3PXP"))
    code = p._mfa_code()
    assert code is not None and len(code) == 6 and code.isdigit()

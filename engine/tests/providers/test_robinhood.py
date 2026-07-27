"""Tests for wbj.providers.robinhood — guardrails first, no real orders.

Every network call is monkeypatched. These tests never touch Robinhood; they
prove the provider fails closed (no confirm / no stop => refused before any
call) and reports an unhedged entry loudly.
"""

import pickle
import tempfile
import types
from pathlib import Path

import httpx
import pytest

import wbj.providers.robinhood as rhmod
from wbj.providers.robinhood import OrderRefused, RobinhoodProvider

_VALID_PICKLE = {
    "token_type": "Bearer", "access_token": "tok",
    "refresh_token": "r", "device_token": "d",
}


class _Settings:
    def __init__(self, user="u", pw="p", totp=None):
        self.rh_username = user
        self.rh_password = pw
        self.rh_mfa_totp = totp


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _fake_rh(monkeypatch, tmp_path=None, *, entry_ok=True, stop_ok=True,
             session=True, buying_power="1234.56", calls=None):
    """Install fakes for robin_stocks + the session so nothing hits the network.

    `session=True` writes a VALID pickle to a temp SESSION_FILE (so `_ensure_login`
    loads it); `session=False` points SESSION_FILE at a missing path. The account
    read (`buying_power`) is served through a stubbed httpx.get.
    """
    calls = calls if calls is not None else {}

    if session:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".pickle")
        pickle.dump(_VALID_PICKLE, f)
        f.close()
        monkeypatch.setattr(rhmod, "SESSION_FILE", Path(f.name))
    else:
        monkeypatch.setattr(
            rhmod, "SESSION_FILE",
            Path(tempfile.gettempdir()) / "wbj_no_such_rh_session.pickle",
        )

    # robin_stocks session setters are no-ops in tests.
    monkeypatch.setattr(rhmod, "update_session", lambda *a, **k: None)
    monkeypatch.setattr(rhmod, "set_login_state", lambda *a, **k: None)

    # Stub the authenticated account read (httpx.get -> accounts payload).
    def fake_get(url, headers=None, timeout=None):
        calls["get"] = url
        return _Resp(200, {"results": [
            {"is_default": True, "buying_power": buying_power, "account_number": "1"},
        ]})
    monkeypatch.setattr(rhmod.httpx, "get", fake_get)

    def order_buy_limit(symbol, quantity, limitPrice, timeInForce="gtc"):
        calls["buy"] = (symbol, quantity, limitPrice, timeInForce)
        return {"id": "entry-1"} if entry_ok else {"detail": "rejected"}

    def order_sell_stop_loss(symbol, quantity, stopPrice, timeInForce="gtc"):
        calls["stop"] = (symbol, quantity, stopPrice, timeInForce)
        return {"id": "stop-1"} if stop_ok else {"detail": "not enough shares"}

    fake = types.SimpleNamespace(
        order_buy_limit=order_buy_limit,
        order_sell_stop_loss=order_sell_stop_loss,
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
    # No creds AND no stored session -> refuse, pointing at the one-time login.
    _fake_rh(monkeypatch, session=False)
    p = RobinhoodProvider(_Settings(user=None))
    with pytest.raises(OrderRefused, match="rh_login"):
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


def test_available_false_without_credentials_or_session(monkeypatch):
    _fake_rh(monkeypatch, session=False)
    assert RobinhoodProvider(_Settings(user=None)).available is False


def test_available_true_with_stored_session_only(monkeypatch):
    # Plan B: a saved session alone enables the provider, no creds in .env.
    _fake_rh(monkeypatch, session=True)
    assert RobinhoodProvider(_Settings(user=None, pw=None)).available is True


def test_buying_power_reads_default_account(monkeypatch, tmp_path):
    _fake_rh(monkeypatch, tmp_path, buying_power="1234.56")
    assert RobinhoodProvider(_Settings()).buying_power() == 1234.56


def test_corrupt_session_refuses_with_guidance(monkeypatch, tmp_path):
    # An empty/corrupt pickle must refuse (pointing at rh_login), never crash.
    bad = tmp_path / "robinhood.pickle"
    bad.write_bytes(b"")  # empty -> EOFError on load
    monkeypatch.setattr(rhmod, "SESSION_FILE", bad)
    monkeypatch.setattr(rhmod, "rh", types.SimpleNamespace())
    p = RobinhoodProvider(_Settings())
    with pytest.raises(OrderRefused, match="corrupta"):
        p.place_long_with_stop("NVDA", 10, 100.0, 98.0, confirm=True)

"""Tests for the Alpaca **paper** provider.

The point of most of these is the safety guard, not the happy path: the danger with
a broker API is not a failed read, it is an order reaching a real account. So the
tests prove the refusals — live key, live mode, wrong host — as hard as the reads.
"""

import httpx
import pytest

from wbj.config import Settings
from wbj.providers.alpaca import PAPER_BASE, AlpacaPaperProvider, NotPaperError

_PAPER_KEY = "PK3ERNRZCK6R44IOH3GK6CJOE7"
_LIVE_KEY = "AK3ERNRZCK6R44IOH3GK6CJOE7"


def _provider(handler=None, key_id=_PAPER_KEY, secret="secret123", paper=True):
    settings = Settings(alpaca_key_id=key_id, alpaca_secret=secret, alpaca_paper=paper)
    client = None
    if handler is not None:
        client = httpx.Client(transport=httpx.MockTransport(handler))
    return AlpacaPaperProvider(settings, client=client)


def _ok(payload):
    def handler(request):
        return httpx.Response(200, json=payload)
    return handler


# -- availability ------------------------------------------------------------


def test_available_needs_both_keys_paper_mode_and_a_paper_key():
    assert _provider().available is True


def test_not_available_without_secret():
    assert _provider(secret=None).available is False


def test_not_available_without_key_id():
    assert _provider(key_id=None).available is False


def test_not_available_with_a_live_key():
    """Una llave AK (real) no habilita el proveedor aunque paper=True."""
    assert _provider(key_id=_LIVE_KEY).available is False


def test_not_available_when_paper_mode_is_off():
    assert _provider(paper=False).available is False


# -- el guardarraíl: se niega ANTES de tocar la red --------------------------


def _exploding_handler(request):
    raise AssertionError(f"no debió salir ninguna petición, y salió a {request.url}")


def test_place_order_refuses_a_live_key_without_sending_anything():
    p = _provider(handler=_exploding_handler, key_id=_LIVE_KEY)
    with pytest.raises(NotPaperError, match="no es de paper"):
        p.place_paper_order("AAPL", 1, "buy")


def test_place_order_refuses_when_paper_mode_is_off_without_sending():
    p = _provider(handler=_exploding_handler, paper=False)
    with pytest.raises(NotPaperError, match="false"):
        p.place_paper_order("AAPL", 1, "buy")


def test_reads_return_none_with_a_live_key_and_never_call_the_network():
    p = _provider(handler=_exploding_handler, key_id=_LIVE_KEY)
    assert p.account() is None
    assert p.positions() is None


# -- toda petición va al host de paper ---------------------------------------


def test_account_hits_the_paper_host_with_auth_headers():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={"equity": "1000", "buying_power": "2000"})

    acct = _provider(handler).account()
    req = captured["request"]
    assert req.url.host == "paper-api.alpaca.markets"
    assert req.headers["APCA-API-KEY-ID"] == _PAPER_KEY
    assert req.headers["APCA-API-SECRET-KEY"] == "secret123"
    assert acct["equity"] == "1000"


def test_the_base_url_is_the_paper_host():
    """Candado de regresión: si alguien cambia PAPER_BASE al host real, esto grita."""
    assert PAPER_BASE == "https://paper-api.alpaca.markets"
    assert "paper-api" in PAPER_BASE


# -- colocar una orden paper -------------------------------------------------


def test_place_paper_order_posts_the_expected_body():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={"id": "abc", "status": "accepted"})

    out = _provider(handler).place_paper_order("nvda", 3, "buy")
    req = captured["request"]
    assert req.method == "POST"
    assert req.url.host == "paper-api.alpaca.markets"
    assert str(req.url).endswith("/v2/orders")
    import json
    body = json.loads(req.content)
    assert body == {"symbol": "NVDA", "qty": "3", "side": "buy",
                    "type": "market", "time_in_force": "day"}
    assert out["status"] == "accepted"


def test_limit_order_includes_the_price():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={"id": "x"})

    _provider(handler).place_paper_order("AAPL", 2, "sell", "limit", limit_price=190.5)
    import json
    body = json.loads(captured["request"].content)
    assert body["type"] == "limit" and body["limit_price"] == "190.5"


def test_limit_order_without_price_is_refused_before_sending():
    p = _provider(handler=_exploding_handler)
    with pytest.raises(ValueError, match="limit_price"):
        p.place_paper_order("AAPL", 1, "buy", "limit")


@pytest.mark.parametrize("bad", [
    {"side": "hold"}, {"order_type": "stop"}, {"time_in_force": "forever"},
])
def test_invalid_arguments_are_refused_before_sending(bad):
    p = _provider(handler=_exploding_handler)
    kwargs = {"side": "buy", **bad}
    with pytest.raises(ValueError):
        p.place_paper_order("AAPL", 1, **kwargs)


def test_negative_quantity_is_refused():
    p = _provider(handler=_exploding_handler)
    with pytest.raises(ValueError, match="qty"):
        p.place_paper_order("AAPL", -5, "buy")


def test_rejected_order_raises_so_a_forward_test_never_assumes_a_fill():
    def handler(request):
        return httpx.Response(422, text="insufficient buying power")

    p = _provider(handler)
    with pytest.raises(RuntimeError, match="rechazó"):
        p.place_paper_order("AAPL", 1, "buy")


# -- lecturas resilientes ----------------------------------------------------


def test_reads_return_none_on_http_error_never_raise():
    def handler(request):
        return httpx.Response(500, text="boom")

    p = _provider(handler)
    assert p.account() is None
    assert p.positions() is None
    assert p.orders() is None


def test_positions_parses_a_list():
    payload = [{"symbol": "NVDA", "qty": "3", "market_value": "600"}]
    assert _provider(_ok(payload)).positions()[0]["symbol"] == "NVDA"

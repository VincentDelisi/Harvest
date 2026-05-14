"""Public.com client tests with mocked HTTP transport.

We use httpx.MockTransport to intercept requests at the transport layer —
no network, full control over responses, exact request inspection.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from engine.broker.auth import TokenManager
from engine.broker.public_client import PublicAPIError, PublicClient
from engine.broker.types import Instrument, OrderLeg


# ───────────────────── Test fixtures ─────────────────────

ACCT = "00000000-0000-0000-0000-000000000001"


def _stub_token_manager() -> TokenManager:
    """A TokenManager that returns a fixed token without hitting the network."""
    tm = TokenManager(secret="fake-secret-for-tests")
    tm._cached = type(  # type: ignore[attr-defined]
        "T", (), {"token": "test-access-token", "expires_at_epoch": 1e18}
    )()
    return tm


def _mock_token_manager(
    handler: Callable[[httpx.Request], httpx.Response]
) -> TokenManager:
    """A TokenManager that uses a MockTransport so /access-tokens is also mocked."""
    return TokenManager(
        secret="fake-secret-for-tests",
        transport=httpx.MockTransport(handler),
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    dry_run: bool = False,
    auth_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> PublicClient:
    transport = httpx.MockTransport(handler)
    if auth_handler is not None:
        token_manager = _mock_token_manager(auth_handler)
    else:
        token_manager = _stub_token_manager()
    client = PublicClient(
        account_id=ACCT,
        base_url="https://api.public.com",
        token_manager=token_manager,
        dry_run=dry_run,
        transport=transport,
    )
    return client


def _json_response(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})


# ───────────────────── Tests ─────────────────────

def test_get_account_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/userapigateway/trading/account"
        assert request.headers["Authorization"] == "Bearer test-access-token"
        return _json_response(200, {
            "accounts": [{
                "accountId": ACCT,
                "accountType": "BROKERAGE",
                "optionsLevel": "LEVEL_2",
                "brokerageAccountType": "MARGIN",
            }]
        })

    client = make_client(handler)
    resp = client.get_account()
    assert len(resp.accounts) == 1
    assert resp.accounts[0].optionsLevel == "LEVEL_2"
    assert resp.accounts[0].brokerageAccountType == "MARGIN"
    client.close()


def test_assert_options_level_passes():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {
            "accounts": [{"accountId": ACCT, "accountType": "BROKERAGE", "optionsLevel": "LEVEL_3"}]
        })
    client = make_client(handler)
    client.assert_options_level_at_least("LEVEL_2")  # no raise
    client.close()


def test_assert_options_level_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {
            "accounts": [{"accountId": ACCT, "accountType": "BROKERAGE", "optionsLevel": "LEVEL_1"}]
        })
    client = make_client(handler)
    with pytest.raises(PublicAPIError):
        client.assert_options_level_at_least("LEVEL_2")
    client.close()


def test_get_option_chain_with_greeks():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/userapigateway/marketdata/{ACCT}/option-chain"
        body = json.loads(request.content)
        assert body["instrument"]["symbol"] == "SPY"
        assert body["expirationDate"] == "2026-05-08"
        return _json_response(200, {
            "baseSymbol": "SPY",
            "calls": [],
            "puts": [
                {
                    "instrument": {"symbol": "SPY260508P00580000", "type": "OPTION"},
                    "outcome": "SUCCESS",
                    "bid": "0.40", "ask": "0.45",
                    "openInterest": "1500", "volume": "300",
                    "delta": "-0.20", "impliedVolatility": "0.18",
                },
            ],
        })

    client = make_client(handler)
    chain = client.get_option_chain("SPY", "2026-05-08")
    assert chain.baseSymbol == "SPY"
    assert len(chain.puts) == 1
    assert chain.puts[0].delta == "-0.20"
    client.close()


def test_preflight_then_place_multi_leg():
    captured = {"preflight_count": 0, "place_count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/preflight/multi-leg"):
            captured["preflight_count"] += 1
            return _json_response(200, {
                "baseSymbol": "SPY",
                "strategyName": "Put Spread",
                "legs": [],
                "orderValue": "100.00",
                "buyingPowerRequirement": "65.00",
            })
        if request.url.path.endswith("/order/multileg"):
            captured["place_count"] += 1
            body = json.loads(request.content)
            return _json_response(200, {"orderId": body["orderId"]})
        return _json_response(404, {})

    client = make_client(handler)
    legs = [
        OrderLeg(
            instrument=Instrument(symbol="SPY260508P00580000", type="OPTION"),
            side="SELL", openCloseIndicator="OPEN", ratioQuantity=1,
        ),
        OrderLeg(
            instrument=Instrument(symbol="SPY260508P00579000", type="OPTION"),
            side="BUY", openCloseIndicator="OPEN", ratioQuantity=1,
        ),
    ]
    resp = client.place_multi_leg_order(legs=legs, limit_price="0.35", quantity=1)
    assert captured["preflight_count"] == 1
    assert captured["place_count"] == 1
    assert resp.orderId  # UUID was generated
    client.close()


def test_dry_run_does_not_call_order_endpoint():
    captured = {"preflight": 0, "place": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/preflight/multi-leg"):
            captured["preflight"] += 1
            return _json_response(200, {
                "baseSymbol": "SPY", "legs": [], "orderValue": "100.00",
            })
        if request.url.path.endswith("/order/multileg"):
            captured["place"] += 1
            return _json_response(200, {"orderId": "should-not-happen"})
        return _json_response(404, {})

    client = make_client(handler, dry_run=True)
    legs = [
        OrderLeg(
            instrument=Instrument(symbol="SPY260508P00580000", type="OPTION"),
            side="SELL", openCloseIndicator="OPEN", ratioQuantity=1,
        ),
        OrderLeg(
            instrument=Instrument(symbol="SPY260508P00579000", type="OPTION"),
            side="BUY", openCloseIndicator="OPEN", ratioQuantity=1,
        ),
    ]
    resp = client.place_multi_leg_order(legs=legs, limit_price="0.35", quantity=1)
    assert captured["preflight"] == 1, "preflight still runs in dry_run (we want validation)"
    assert captured["place"] == 0, "order endpoint MUST NOT be hit in dry_run"
    assert resp.orderId  # fake UUID returned
    client.close()


def test_get_order_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "/order/abc-123" in request.url.path
        return _json_response(200, {
            "orderId": "abc-123",
            "status": "FILLED",
            "filledQuantity": "1",
            "averagePrice": "0.34",
        })

    client = make_client(handler)
    status = client.get_order_status("abc-123")
    assert status.status == "FILLED"
    assert status.averagePrice == "0.34"
    client.close()


def test_cancel_order_dry_run_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("cancel must not hit the network in dry_run")
    client = make_client(handler, dry_run=True)
    client.cancel_order("abc-123")  # no exception
    client.close()


def test_get_portfolio():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {
            "accountId": ACCT,
            "accountType": "BROKERAGE",
            "buyingPower": {"buyingPower": "10000.00", "optionsBuyingPower": "10000.00"},
            "equity": [{"type": "CASH", "value": "10000.00"}],
            "positions": [],
            "orders": [],
        })
    client = make_client(handler)
    p = client.get_portfolio()
    assert p.buyingPower.buyingPower == "10000.00"
    client.close()


def test_retries_on_500_then_succeeds():
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] < 2:
            return _json_response(500, {"error": "transient"})
        return _json_response(200, {
            "accounts": [{"accountId": ACCT, "accountType": "BROKERAGE"}]
        })

    client = make_client(handler)
    resp = client.get_account()
    assert state["calls"] == 2
    assert resp.accounts[0].accountId == ACCT
    client.close()


def test_401_invalidates_token_then_retries():
    state = {"data_calls": 0, "auth_calls": 0}

    def auth_handler(request: httpx.Request) -> httpx.Response:
        state["auth_calls"] += 1
        return _json_response(200, {"accessToken": f"token-v{state['auth_calls']}"})

    def data_handler(request: httpx.Request) -> httpx.Response:
        state["data_calls"] += 1
        # First call: 401 (token allegedly stale)
        if state["data_calls"] < 2:
            return _json_response(401, {"error": "expired"})
        return _json_response(200, {
            "accounts": [{"accountId": ACCT, "accountType": "BROKERAGE"}]
        })

    client = make_client(data_handler, auth_handler=auth_handler)
    resp = client.get_account()
    # Should have minted at least twice (initial + after invalidation)
    assert state["auth_calls"] >= 2
    assert resp.accounts[0].accountId == ACCT
    client.close()


def test_4xx_other_than_401_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(400, {"error": "bad request"})
    client = make_client(handler)
    with pytest.raises(PublicAPIError) as exc:
        client.get_account()
    assert exc.value.status == 400
    client.close()


def test_instrument_accepts_multi_leg_instrument_type():
    """Public.com returns instrument.type='MULTI_LEG_INSTRUMENT' on order status
    responses for credit spreads. Must parse cleanly without validation error."""
    inst = Instrument(symbol="SPY-MULTI-LEG-123", type="MULTI_LEG_INSTRUMENT")
    assert inst.type == "MULTI_LEG_INSTRUMENT"

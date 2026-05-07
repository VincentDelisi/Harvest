"""OCC option symbol encode/decode tests."""
from __future__ import annotations

from datetime import date

import pytest

from engine.broker.occ import OccOption, decode, encode


def test_encode_call():
    sym = encode("AAPL", date(2024, 2, 16), "CALL", 140.0)
    assert sym == "AAPL240216C00140000"


def test_encode_put():
    sym = encode("SPY", date(2026, 5, 8), "PUT", 580.0)
    assert sym == "SPY260508P00580000"


def test_encode_fractional_strike():
    sym = encode("SPY", date(2026, 5, 8), "PUT", 580.5)
    assert sym == "SPY260508P00580500"


def test_decode_roundtrip_call():
    sym = "AAPL240216C00140000"
    opt = decode(sym)
    assert opt.root == "AAPL"
    assert opt.expiration == date(2024, 2, 16)
    assert opt.option_type == "CALL"
    assert opt.strike == 140.0
    assert opt.symbol == sym


def test_decode_roundtrip_put_fractional():
    sym = "SPY260508P00580500"
    opt = decode(sym)
    assert opt.option_type == "PUT"
    assert opt.strike == 580.5


def test_decode_invalid():
    with pytest.raises(ValueError):
        decode("not-a-symbol")


def test_encode_rejects_negative_strike():
    with pytest.raises(ValueError):
        encode("SPY", date(2026, 1, 1), "CALL", -5)


def test_encode_rejects_bad_type():
    with pytest.raises(ValueError):
        encode("SPY", date(2026, 1, 1), "FOO", 100.0)  # type: ignore[arg-type]

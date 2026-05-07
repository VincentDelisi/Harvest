"""Spread builder tests — verify strike selection, gate enforcement, and best-pick logic."""
from __future__ import annotations

from engine.broker.spread_builder import build_credit_spread, closing_legs
from engine.broker.types import Instrument, OptionChainEntry, OptionChainResponse


def _put(strike: float, *, bid: str, ask: str, oi: str, delta: str) -> OptionChainEntry:
    sym = f"SPY260508P00{int(strike*1000):06d}"
    return OptionChainEntry(
        instrument=Instrument(symbol=sym, type="OPTION"),
        outcome="SUCCESS",
        bid=bid, ask=ask,
        openInterest=oi,
        delta=delta,
        strikePrice=f"{strike:.2f}",
    )


def _call(strike: float, *, bid: str, ask: str, oi: str, delta: str) -> OptionChainEntry:
    sym = f"SPY260508C00{int(strike*1000):06d}"
    return OptionChainEntry(
        instrument=Instrument(symbol=sym, type="OPTION"),
        outcome="SUCCESS",
        bid=bid, ask=ask,
        openInterest=oi,
        delta=delta,
        strikePrice=f"{strike:.2f}",
    )


def test_picks_put_spread_with_short_delta_in_band():
    chain = OptionChainResponse(
        baseSymbol="SPY",
        calls=[],
        puts=[
            # Tight bid-ask (≤2%) like real SPY weekly options
            _put(580, bid="0.40", ask="0.41", oi="1500", delta="-0.20"),  # short candidate
            _put(579, bid="0.04", ask="0.05", oi="1500", delta="-0.10"),  # long
            _put(581, bid="0.55", ask="0.56", oi="1500", delta="-0.30"),  # too high delta
            _put(578, bid="0.02", ask="0.03", oi="1500", delta="-0.07"),
        ],
    )
    spread = build_credit_spread(chain, "SPY", "PUT", "2026-05-08", width=1.0)
    assert spread is not None
    assert spread.short_strike == 580
    assert spread.long_strike == 579
    assert spread.short_delta == 0.20
    # Net credit at mid: (0.40+0.41)/2 - (0.04+0.05)/2 = 0.405 - 0.045 = 0.36
    assert spread.net_credit == 0.36
    assert spread.credit_to_width >= 0.33
    assert len(spread.legs) == 2
    assert spread.legs[0].side == "SELL"
    assert spread.legs[1].side == "BUY"


def test_picks_call_spread_in_bear_regime():
    chain = OptionChainResponse(
        baseSymbol="SPY",
        calls=[
            _call(580, bid="0.40", ask="0.41", oi="1500", delta="0.20"),  # short candidate
            _call(581, bid="0.04", ask="0.05", oi="1500", delta="0.10"),  # long (HIGHER strike)
        ],
        puts=[],
    )
    spread = build_credit_spread(chain, "SPY", "CALL", "2026-05-08", width=1.0)
    assert spread is not None
    assert spread.direction == "CALL"
    assert spread.short_strike == 580
    assert spread.long_strike == 581


def test_rejects_when_credit_below_threshold():
    # Short premium too low → credit/width < 0.33
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.10", ask="0.12", oi="1500", delta="-0.20"),
            _put(579, bid="0.04", ask="0.06", oi="1500", delta="-0.10"),
        ],
    )
    spread = build_credit_spread(chain, "SPY", "PUT", "2026-05-08", width=1.0)
    assert spread is None  # 0.06 mid credit on $1 width = 6% < 33%


def test_rejects_low_open_interest():
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.40", ask="0.45", oi="100", delta="-0.20"),  # OI < 500
            _put(579, bid="0.05", ask="0.08", oi="1500", delta="-0.10"),
        ],
    )
    assert build_credit_spread(chain, "SPY", "PUT", "2026-05-08", width=1.0) is None


def test_rejects_wide_bid_ask():
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.20", ask="0.60", oi="1500", delta="-0.20"),  # 0.40 spread / 0.40 mid = 100%
            _put(579, bid="0.05", ask="0.08", oi="1500", delta="-0.10"),
        ],
    )
    assert build_credit_spread(chain, "SPY", "PUT", "2026-05-08", width=1.0) is None


def test_rejects_short_delta_out_of_band():
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            # Only available short is at delta 0.40 → outside [0.16, 0.25]
            _put(580, bid="0.80", ask="0.85", oi="1500", delta="-0.40"),
            _put(579, bid="0.40", ask="0.45", oi="1500", delta="-0.30"),
        ],
    )
    assert build_credit_spread(chain, "SPY", "PUT", "2026-05-08", width=1.0) is None


def test_picks_best_credit_to_width_when_multiple_candidates():
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            # Two valid short candidates with different credits (tight bid-ask)
            _put(580, bid="0.40", ask="0.41", oi="1500", delta="-0.20"),  # mid 0.405
            _put(579, bid="0.04", ask="0.05", oi="1500", delta="-0.10"),  # long for 580
            _put(581, bid="0.50", ask="0.51", oi="1500", delta="-0.25"),  # mid 0.505
        ],
    )
    # 581/580 spread → credit ≈ 0.505 - 0.405 = 0.10 (10% of width — fails 33% gate)
    # 580/579 spread → credit ≈ 0.405 - 0.045 = 0.36 (36% of width — passes) ← picked
    spread = build_credit_spread(chain, "SPY", "PUT", "2026-05-08", width=1.0)
    assert spread is not None
    assert spread.short_strike == 580


def test_closing_legs_are_close_with_swapped_sides():
    legs = closing_legs("SPY260508P00580000", "SPY260508P00579000")
    assert legs[0].side == "BUY" and legs[0].openCloseIndicator == "CLOSE"
    assert legs[1].side == "SELL" and legs[1].openCloseIndicator == "CLOSE"

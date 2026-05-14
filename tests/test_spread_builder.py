"""Spread builder tests — verify strike selection, gate enforcement, and best-pick logic."""
from __future__ import annotations

from engine.broker.spread_builder import (
    build_credit_spread,
    build_credit_spread_adaptive,
    closing_legs,
)
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


# ----- Adaptive-width fallback ($1 → $2 → $3 → $5) -------------------------


def test_adaptive_picks_dollar_one_when_credit_is_rich():
    """$1 width clears 33% gate → adaptive should select it without widening."""
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.40", ask="0.41", oi="1500", delta="-0.20"),
            _put(579, bid="0.04", ask="0.05", oi="1500", delta="-0.10"),
        ],
    )
    report = build_credit_spread_adaptive(
        chain, "SPY", "PUT", "2026-05-08",
        widths_to_try=(1.0, 2.0, 3.0, 5.0),
    )
    assert report.candidate is not None
    assert report.selected_width == 1.0
    assert report.candidate.width == 1.0
    # Only $1 was tried because it succeeded on the first attempt.
    assert len(report.per_width) == 1
    assert report.per_width[0].width == 1.0
    assert report.per_width[0].accepted == 1


def test_adaptive_falls_back_to_dollar_two_when_dollar_one_credit_too_thin():
    """Real-world scenario from May 8 logs: low-vol environment where the
    short strike has plenty of OI and tight bid-ask, but the $1 spread credit
    falls below the 33% floor. Widening to $2 keeps the same delta but
    clears the credit gate.
    """
    # Short premium mid 0.805 at delta 0.20:
    #   $1-away long mid 0.555 → $1 credit 0.25 / $1 = 25%  (FAIL: <33%)
    #   $2-away long mid 0.105 → $2 credit 0.70 / $2 = 35%  (PASS)
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.80", ask="0.81", oi="1500", delta="-0.20"),  # short, mid 0.805
            _put(579, bid="0.55", ask="0.56", oi="1500", delta="-0.15"),  # $1-long, mid 0.555 → credit 0.25
            _put(578, bid="0.10", ask="0.11", oi="1500", delta="-0.08"),  # $2-long, mid 0.105 → credit 0.70
        ],
    )
    report = build_credit_spread_adaptive(
        chain, "SPY", "PUT", "2026-05-08",
        widths_to_try=(1.0, 2.0, 3.0, 5.0),
    )
    assert report.candidate is not None
    assert report.selected_width == 2.0
    assert report.candidate.width == 2.0
    assert report.candidate.short_strike == 580
    assert report.candidate.long_strike == 578
    # Two widths attempted: $1 failed, $2 succeeded. $3/$5 not tried.
    assert len(report.per_width) == 2
    assert report.per_width[0].width == 1.0
    assert report.per_width[0].examined == 1
    assert report.per_width[0].credit_too_thin == 1
    assert report.per_width[0].accepted == 0
    assert report.per_width[1].width == 2.0
    assert report.per_width[1].accepted == 1


def test_adaptive_returns_none_when_all_widths_fail():
    """No matching long strike at any width → returns None with full tally."""
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            # Short candidate exists but no further-OTM strikes are quoted
            _put(580, bid="0.40", ask="0.41", oi="1500", delta="-0.20"),
        ],
    )
    report = build_credit_spread_adaptive(
        chain, "SPY", "PUT", "2026-05-08",
        widths_to_try=(1.0, 2.0, 3.0, 5.0),
    )
    assert report.candidate is None
    assert report.selected_width is None
    # All four widths attempted, all failed at the no_long_strike gate.
    assert len(report.per_width) == 4
    for c in report.per_width:
        assert c.examined == 1
        assert c.no_long_strike == 1
        assert c.accepted == 0
    assert [c.width for c in report.per_width] == [1.0, 2.0, 3.0, 5.0]


def test_adaptive_short_illiquid_counted_at_first_gate():
    """Candidate inside delta band but with sub-min OI — should be tallied
    as short_illiquid (not credit_too_thin) since OI is the first failure.
    """
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.40", ask="0.41", oi="100", delta="-0.20"),  # OI<500
            _put(579, bid="0.04", ask="0.05", oi="1500", delta="-0.10"),
        ],
    )
    report = build_credit_spread_adaptive(
        chain, "SPY", "PUT", "2026-05-08",
        widths_to_try=(1.0, 2.0),
    )
    assert report.candidate is None
    # First failure recorded — short_illiquid, not credit_too_thin
    assert report.per_width[0].examined == 1
    assert report.per_width[0].short_illiquid == 1
    assert report.per_width[0].credit_too_thin == 0


def test_adaptive_uses_config_default_when_widths_to_try_omitted():
    """When called without widths_to_try, adaptive uses CONFIG.widths_to_try."""
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.40", ask="0.41", oi="1500", delta="-0.20"),
            _put(579, bid="0.04", ask="0.05", oi="1500", delta="-0.10"),
        ],
    )
    report = build_credit_spread_adaptive(chain, "SPY", "PUT", "2026-05-08")
    assert report.candidate is not None
    assert report.selected_width == 1.0


def test_adaptive_empty_widths_returns_empty_report():
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[_put(580, bid="0.40", ask="0.41", oi="1500", delta="-0.20")],
    )
    report = build_credit_spread_adaptive(
        chain, "SPY", "PUT", "2026-05-08", widths_to_try=()
    )
    assert report.candidate is None
    assert report.selected_width is None
    assert report.per_width == []


def test_limit_price_str_is_negative_for_credit_spread():
    """Public.com API requires credit spread limit prices to be negative.
    Their preflight rejects positive limits with code 104:
    'Limit price must be negative for credit spreads.'"""
    chain = OptionChainResponse(
        baseSymbol="SPY", calls=[],
        puts=[
            _put(580, bid="0.40", ask="0.41", oi="1500", delta="-0.20"),
            _put(579, bid="0.04", ask="0.05", oi="1500", delta="-0.10"),
        ],
    )
    report = build_credit_spread_adaptive(chain, "SPY", "PUT", "2026-05-08")
    cand = report.candidate
    assert cand is not None
    s = cand.limit_price_str()
    assert s.startswith("-"), f"expected negative limit price, got {s!r}"
    assert float(s) < 0, f"expected float < 0, got {s!r}"
    # Magnitude should equal the net_credit
    assert abs(float(s) + cand.net_credit) < 1e-9

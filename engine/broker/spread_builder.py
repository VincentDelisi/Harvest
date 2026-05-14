"""Build credit-spread orders from option-chain data.

Encapsulates the strike-selection logic per STRATEGY_SPEC.md §4:
  - Bull regime → put credit spread (sell higher-strike put, buy lower-strike put)
  - Bear regime → call credit spread (sell lower-strike call, buy higher-strike call)
  - Short strike delta in [0.16, 0.25]
  - Width = $1
  - Net credit ≥ 33% of width
  - Bid-ask spread on each leg ≤ 10% of mid
  - Open interest ≥ 500 on each strike

Returns a fully-formed list[OrderLeg] ready to pass to PublicClient.preflight_multi_leg().
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine.broker.types import Instrument, OptionChainEntry, OptionChainResponse, OrderLeg
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)


def _parse_or_none(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def _bid_ask_pct(bid: float | None, ask: float | None) -> float | None:
    m = _mid(bid, ask)
    if m is None or m <= 0 or bid is None or ask is None:
        return None
    return (ask - bid) / m


@dataclass
class GateFailureCounts:
    """Per-width tally of why short-strike candidates failed each filter.

    Used by diagnostics to show which gate is the actual bottleneck on a
    given attempt. Counts each candidate at most once — the first failed
    filter is what's recorded.
    """
    width: float
    examined: int = 0           # short-strike candidates in [delta_min, delta_max]
    short_illiquid: int = 0     # OI<min, bid/ask<=0, or bid-ask>10%
    no_long_strike: int = 0     # paired strike not found in chain
    long_illiquid: int = 0      # paired strike failed liquidity
    credit_too_thin: int = 0    # credit/width below 33%
    accepted: int = 0           # all gates passed


@dataclass
class SpreadBuilderReport:
    """Outcome of `build_credit_spread_adaptive` — the selected candidate
    (if any) plus a per-width failure tally for diagnostics."""
    candidate: Optional["SpreadCandidate"]
    selected_width: Optional[float]
    per_width: list[GateFailureCounts]


@dataclass
class SpreadCandidate:
    """A fully-vetted credit spread ready to send to preflight."""
    underlying: str
    direction: str  # "PUT" | "CALL"
    short_strike: float
    long_strike: float
    width: float
    short_symbol: str
    long_symbol: str
    short_delta: float
    short_bid: float
    short_ask: float
    long_bid: float
    long_ask: float
    short_oi: int
    long_oi: int
    expiration: str
    net_credit: float        # mid-price credit for the spread
    credit_to_width: float   # ratio
    legs: list[OrderLeg]

    def limit_price_str(self) -> str:
        """Limit price for the spread, formatted to 2 decimals.

        Public.com convention: for credit spreads (cash received by trader)
        the API expects the limit price as a NEGATIVE number. Their preflight
        rejects positive limit prices with: 'Limit price must be negative for
        credit spreads.' (error code 104).

        Our `net_credit` is stored as a positive float, so we negate here on
        the way out to the broker.
        """
        return f"-{self.net_credit:.2f}"


def _ok_short_liquidity(entry: OptionChainEntry) -> bool:
    """Liquidity gate for the short leg — strict % gate (STRATEGY_SPEC §4.3)."""
    bid = _parse_or_none(entry.bid)
    ask = _parse_or_none(entry.ask)
    oi = _parse_or_none(entry.openInterest)
    pct = _bid_ask_pct(bid, ask)

    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False
    if oi is None or oi < CONFIG.min_open_interest:
        return False
    if pct is None or pct > CONFIG.max_bid_ask_pct:
        return False
    return True


def _ok_long_liquidity(entry: OptionChainEntry) -> bool:
    """Liquidity gate for the long leg — absolute-$ gate because far-OTM mids are tiny."""
    bid = _parse_or_none(entry.bid)
    ask = _parse_or_none(entry.ask)
    oi = _parse_or_none(entry.openInterest)

    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False
    if oi is None or oi < CONFIG.min_open_interest:
        return False
    if (ask - bid) > CONFIG.max_long_leg_abs_spread:
        return False
    return True


def _short_delta_ok(delta: float, direction: str) -> bool:
    """Short strike delta must be in [δmin, δmax].
    For puts, Public returns negative deltas — use absolute value."""
    return CONFIG.delta_min <= abs(delta) <= CONFIG.delta_max


def _build_for_width(
    chain: OptionChainResponse,
    underlying: str,
    direction: str,
    expiration: str,
    width: float,
) -> tuple[Optional[SpreadCandidate], GateFailureCounts]:
    """Inner builder for a single width. Returns the best candidate (if any)
    plus a tally of where short-strike candidates dropped out.

    Each delta-band candidate is counted at most once — at the *first* gate
    it failed.
    """
    counts = GateFailureCounts(width=width)

    if direction not in ("PUT", "CALL"):
        raise ValueError(f"direction must be PUT or CALL, got {direction}")

    entries = chain.puts if direction == "PUT" else chain.calls
    if not entries:
        log.info("No %s entries in chain for %s %s", direction, underlying, expiration)
        return None, counts

    # Index entries by strike for fast pairing
    by_strike: dict[float, OptionChainEntry] = {}
    for e in entries:
        # Public returns strikePrice as a string OR embeds it in the OCC symbol;
        # try both
        strike = _parse_or_none(e.strikePrice)
        if strike is None:
            try:
                from engine.broker.occ import decode
                strike = decode(e.instrument.symbol).strike
            except Exception:  # noqa: BLE001
                continue
        by_strike[strike] = e

    if not by_strike:
        return None, counts

    # Find the short-strike candidate(s): delta in band, liquid
    candidates: list[SpreadCandidate] = []
    for short_strike, short_entry in by_strike.items():
        delta = _parse_or_none(short_entry.delta)
        if delta is None:
            continue
        if not _short_delta_ok(delta, direction):
            continue

        # Inside delta band — this is a real candidate to evaluate.
        counts.examined += 1

        if not _ok_short_liquidity(short_entry):
            counts.short_illiquid += 1
            continue

        # Long leg: lower strike for puts, higher strike for calls (further OTM)
        if direction == "PUT":
            long_strike = round(short_strike - width, 2)
        else:
            long_strike = round(short_strike + width, 2)
        long_entry = by_strike.get(long_strike)
        if long_entry is None:
            counts.no_long_strike += 1
            continue
        if not _ok_long_liquidity(long_entry):
            counts.long_illiquid += 1
            continue

        # Net credit at mid: short premium - long premium (both are positive)
        s_bid = _parse_or_none(short_entry.bid) or 0.0
        s_ask = _parse_or_none(short_entry.ask) or 0.0
        l_bid = _parse_or_none(long_entry.bid) or 0.0
        l_ask = _parse_or_none(long_entry.ask) or 0.0
        s_mid = (s_bid + s_ask) / 2.0
        l_mid = (l_bid + l_ask) / 2.0
        net_credit = round(s_mid - l_mid, 2)
        if net_credit <= 0:
            counts.credit_too_thin += 1
            continue
        ratio = net_credit / width
        if ratio < CONFIG.min_credit_to_width:
            counts.credit_too_thin += 1
            continue

        counts.accepted += 1
        legs = [
            OrderLeg(
                instrument=Instrument(symbol=short_entry.instrument.symbol, type="OPTION"),
                side="SELL",
                openCloseIndicator="OPEN",
                ratioQuantity=1,
            ),
            OrderLeg(
                instrument=Instrument(symbol=long_entry.instrument.symbol, type="OPTION"),
                side="BUY",
                openCloseIndicator="OPEN",
                ratioQuantity=1,
            ),
        ]

        candidates.append(
            SpreadCandidate(
                underlying=underlying,
                direction=direction,
                short_strike=short_strike,
                long_strike=long_strike,
                width=width,
                short_symbol=short_entry.instrument.symbol,
                long_symbol=long_entry.instrument.symbol,
                short_delta=abs(delta),
                short_bid=s_bid, short_ask=s_ask,
                long_bid=l_bid, long_ask=l_ask,
                short_oi=int(_parse_or_none(short_entry.openInterest) or 0),
                long_oi=int(_parse_or_none(long_entry.openInterest) or 0),
                expiration=expiration,
                net_credit=net_credit,
                credit_to_width=ratio,
                legs=legs,
            )
        )

    if not candidates:
        return None, counts

    # Pick highest credit/width. Tie-break: short delta closest to 0.20.
    candidates.sort(key=lambda c: (-c.credit_to_width, abs(c.short_delta - 0.20)))
    return candidates[0], counts


def build_credit_spread(
    chain: OptionChainResponse,
    underlying: str,
    direction: str,  # "PUT" or "CALL"
    expiration: str,
    width: float = 1.0,
) -> Optional[SpreadCandidate]:
    """Find the best credit spread that satisfies all gates at a single width.

    "Best" = highest credit/width among the deltas in the [0.16, 0.25] band
    that also passes liquidity gates.

    Backward-compatible single-width API. For the adaptive $1→$2→$3→$5
    fallback, use `build_credit_spread_adaptive` instead.
    """
    best, _counts = _build_for_width(chain, underlying, direction, expiration, width)
    if best is None:
        log.info("No %s spread candidates for %s exp=%s passed all gates (width=%.2f)",
                 direction, underlying, expiration, width)
        return None
    log.info(
        "Selected %s spread: %s/%s short_delta=%.2f credit=%.2f credit/width=%.0f%%",
        direction, best.short_symbol, best.long_symbol,
        best.short_delta, best.net_credit, best.credit_to_width * 100,
    )
    return best


def build_credit_spread_adaptive(
    chain: OptionChainResponse,
    underlying: str,
    direction: str,
    expiration: str,
    widths_to_try: Optional[tuple[float, ...]] = None,
) -> SpreadBuilderReport:
    """Try widths in order until one yields a passing candidate.

    Strategy: $1 spreads have the best risk/reward when premium is rich, so
    we try the tightest width first and fall back to wider spreads only when
    the credit/width floor can't be cleared. Delta band stays the same at all
    widths, so directional risk is unchanged — wider spreads only increase
    capital at risk per contract while preserving win rate.

    Returns a `SpreadBuilderReport` with the selected candidate (or None if
    nothing passed at any width) plus a per-width gate-failure breakdown for
    diagnostics.
    """
    widths = widths_to_try if widths_to_try is not None else CONFIG.widths_to_try
    if not widths:
        return SpreadBuilderReport(candidate=None, selected_width=None, per_width=[])

    per_width: list[GateFailureCounts] = []
    for w in widths:
        cand, counts = _build_for_width(chain, underlying, direction, expiration, w)
        per_width.append(counts)
        if cand is not None:
            log.info(
                "Adaptive: selected %s spread at width=$%.2f — %s/%s "
                "short_delta=%.2f credit=%.2f credit/width=%.0f%% "
                "(tried widths up to here: %s)",
                direction, w, cand.short_symbol, cand.long_symbol,
                cand.short_delta, cand.net_credit, cand.credit_to_width * 100,
                [pw.width for pw in per_width],
            )
            return SpreadBuilderReport(
                candidate=cand, selected_width=w, per_width=per_width
            )

    log.info(
        "Adaptive: no %s spread passed at any width for %s exp=%s. Tally: %s",
        direction, underlying, expiration,
        [(c.width, c.examined, c.accepted) for c in per_width],
    )
    return SpreadBuilderReport(candidate=None, selected_width=None, per_width=per_width)


def closing_legs(short_symbol: str, long_symbol: str) -> list[OrderLeg]:
    """Build the legs to CLOSE an open credit spread.
    To close, we BUY back the short and SELL the long — both with openCloseIndicator=CLOSE."""
    return [
        OrderLeg(
            instrument=Instrument(symbol=short_symbol, type="OPTION"),
            side="BUY",
            openCloseIndicator="CLOSE",
            ratioQuantity=1,
        ),
        OrderLeg(
            instrument=Instrument(symbol=long_symbol, type="OPTION"),
            side="SELL",
            openCloseIndicator="CLOSE",
            ratioQuantity=1,
        ),
    ]

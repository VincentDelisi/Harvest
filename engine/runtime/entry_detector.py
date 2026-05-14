"""Entry detector — runs during the 10:00–11:30 ET window and watches for triggers.

For each underlying, it polls 5-min bars and computes RSI(2). When RSI dips
below `rsi_oversold` (puts in BULL) or spikes above `rsi_overbought` (calls in BEAR),
it builds a credit spread, runs preflight, and submits the order.

Concurrency / risk caps are enforced before order submission:
  - max_positions_per_underlying
  - max_total_positions
  - max_aggregate_max_loss_pct of equity

This module does NOT manage exits — that's PositionMonitor's job.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

import pandas as pd
import pytz

from engine.broker.public_client import PublicAPIError, PublicClient
from engine.broker.spread_builder import (
    SpreadBuilderReport,
    SpreadCandidate,
    build_credit_spread_adaptive,
)
from engine.notify.discord import Notifier
from engine.runtime.market_context import MarketContext
from engine.runtime.sizing import (
    aggregate_max_loss_pct,
    per_spread_max_loss,
    size_position,
)
from engine.state.store import StateStore, TradeRecord
from engine.strategy.indicators import latest_value, rsi_wilder
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class EntryDecision:
    """Outcome of one check-cycle for one underlying."""
    symbol: str
    direction: Optional[str]    # "PUT" / "CALL" / None
    triggered: bool
    rsi_value: Optional[float]
    blocked_reason: Optional[str]
    candidate: Optional[SpreadCandidate]
    placed_order_id: Optional[str]
    # Per-width spread-builder breakdown when RSI triggered. None when the
    # builder wasn't reached (e.g. regime/IV/RSI gate blocked first).
    builder_report: Optional[SpreadBuilderReport] = None


class EntryDetector:
    def __init__(
        self,
        public_client: PublicClient,
        state: StateStore,
        notifier: Notifier,
    ) -> None:
        self.public = public_client
        self.state = state
        self.notify = notifier

    # ─────────────── window ─────────────────────────────────────────────

    @staticmethod
    def _parse_hhmm(s: str) -> time:
        h, m = s.split(":")
        return time(int(h), int(m))

    def in_entry_window(self, now_et: datetime) -> bool:
        start = self._parse_hhmm(CONFIG.entry_window_start)
        end = self._parse_hhmm(CONFIG.entry_window_end)
        return start <= now_et.time() <= end

    # ─────────────── trigger ────────────────────────────────────────────

    def _rsi_signal(self, bars_5m: pd.DataFrame, direction: str) -> tuple[bool, Optional[float]]:
        """Return (triggered, rsi_value) for the current 5-min bar."""
        if bars_5m.empty or "close" not in bars_5m.columns or len(bars_5m) < 3:
            return False, None
        rsi = rsi_wilder(bars_5m["close"], period=2)
        v = latest_value(rsi)
        if v is None:
            return False, None
        if direction == "PUT" and v < CONFIG.rsi_oversold:
            return True, v
        if direction == "CALL" and v > CONFIG.rsi_overbought:
            return True, v
        return False, v

    # ─────────────── concurrency / risk caps ────────────────────────────

    def _open_max_losses(self) -> list[float]:
        return [
            per_spread_max_loss(t.width, t.credit_received) * t.quantity
            for t in self.state.open_trades()
        ]

    def _can_take_new_position(
        self,
        symbol: str,
        proposed_max_loss: float,
        account_equity: float,
    ) -> tuple[bool, str]:
        open_trades = self.state.open_trades()
        if len(open_trades) >= CONFIG.max_total_positions:
            return False, f"max_total_positions={CONFIG.max_total_positions} reached"
        per_under = sum(1 for t in open_trades if t.underlying == symbol)
        if per_under >= CONFIG.max_positions_per_underlying:
            return False, f"max_positions_per_underlying for {symbol} reached"
        agg = aggregate_max_loss_pct(self._open_max_losses(), proposed_max_loss, account_equity)
        if agg > CONFIG.max_aggregate_max_loss_pct:
            return False, f"aggregate max-loss would be {agg*100:.1f}% (cap {CONFIG.max_aggregate_max_loss_pct*100:.1f}%)"
        return True, "OK"

    # ─────────────── DTE → expiration ───────────────────────────────────

    @staticmethod
    def _trading_dte(today: date, exp_date: date) -> int:
        """Count weekdays (Mon-Fri) between today (exclusive) and expiration
        (inclusive). Approximates trading days; ignores NYSE holidays which
        is acceptable for a 2-3 DTE band because the only common holiday
        that could land in-band is Good Friday and we'd still get a valid
        Thu or Mon exp.

        Examples (Mon=0 ... Fri=4, Sat=5, Sun=6):
          Mon -> Wed:  2 trading days
          Tue -> Thu:  2 trading days
          Wed -> Fri:  2 trading days
          Thu -> Mon:  2 trading days  (weekend skipped, was 4 calendar)
          Fri -> Mon:  1 trading day   (was 3 calendar)
          Fri -> Tue:  2 trading days  (was 4 calendar)
        """
        if exp_date <= today:
            return 0
        days = 0
        cursor = today
        while cursor < exp_date:
            cursor = cursor + timedelta(days=1)
            if cursor.weekday() < 5:  # Mon-Fri = 0-4
                days += 1
        return days

    def _pick_expiration(self, symbol: str, today_et: date) -> Optional[str]:
        """Pick an expiration matching DTE band [dte_min, dte_max].

        DTE is counted in TRADING DAYS, not calendar days. This avoids the
        Thursday-dead-zone bug where the 2-3 calendar-day band fell on
        weekends (Sat/Sun) and no expirations could ever be found.
        """
        try:
            exp_resp = self.public.get_option_expirations(symbol)
        except PublicAPIError as exc:
            log.error("Failed to fetch expirations for %s: %s", symbol, exc)
            return None
        for exp_str in exp_resp.expirations:
            try:
                exp_date = date.fromisoformat(exp_str)
            except Exception:  # noqa: BLE001
                continue
            dte = self._trading_dte(today_et, exp_date)
            if CONFIG.dte_min <= dte <= CONFIG.dte_max:
                return exp_str
        log.info(
            "No expirations in trading-day DTE band [%d,%d] for %s",
            CONFIG.dte_min, CONFIG.dte_max, symbol,
        )
        return None

    # ─────────────── main check ─────────────────────────────────────────

    def check_underlying(
        self,
        symbol: str,
        bars_5m: pd.DataFrame,
        market: MarketContext,
        account_equity: float,
        now_et: datetime,
    ) -> EntryDecision:
        """Evaluate one underlying for entry. Returns the decision.

        bars_5m is fetched by the engine main loop and passed in (so this is
        deterministic and easily testable).
        """
        # 1. Determine direction from regime
        ctx = market.underlyings.get(symbol)
        if ctx is None:
            return EntryDecision(symbol, None, False, None, "no market context", None, None)
        if ctx.trades_puts:
            direction = "PUT"
        elif ctx.trades_calls:
            direction = "CALL"
        else:
            return EntryDecision(symbol, None, False, None, "regime MIXED", None, None)

        # 2. Combined entry gate (event/VIX/IV/regime)
        ok, why = market.is_tradeable(symbol, direction)
        if not ok:
            return EntryDecision(symbol, direction, False, None, why, None, None)

        # 3. RSI(2) trigger
        triggered, rsi_v = self._rsi_signal(bars_5m, direction)
        if not triggered:
            return EntryDecision(symbol, direction, False, rsi_v, "no RSI trigger", None, None)

        log.info("RSI(2) trigger on %s %s: rsi=%.2f", symbol, direction, rsi_v or 0)

        # 4. Pick expiration
        exp = self._pick_expiration(symbol, now_et.date())
        if exp is None:
            return EntryDecision(symbol, direction, True, rsi_v, "no eligible expiration", None, None)

        # 5. Build the spread
        try:
            chain = self.public.get_option_chain(symbol, exp)
        except PublicAPIError as exc:
            log.error("Chain fetch failed for %s %s: %s", symbol, exp, exc)
            return EntryDecision(symbol, direction, True, rsi_v, f"chain fetch failed: {exc}", None, None)

        report = build_credit_spread_adaptive(chain, symbol, direction, exp)
        cand = report.candidate
        if cand is None:
            return EntryDecision(
                symbol, direction, True, rsi_v,
                "no spread passed gates", None, None,
                builder_report=report,
            )

        # 6. Sizing
        qty = size_position(
            account_equity=account_equity,
            width=cand.width,
            credit=cand.net_credit,
            mode=CONFIG.mode,  # type: ignore[arg-type]
            filled_trade_count=self.state.filled_trade_count(),
        )
        if qty <= 0:
            return EntryDecision(
                symbol, direction, True, rsi_v, "sizing returned 0 contracts",
                cand, None, builder_report=report,
            )

        # 7. Concurrency / aggregate-loss cap
        proposed_max_loss = per_spread_max_loss(cand.width, cand.net_credit) * qty
        ok, why = self._can_take_new_position(symbol, proposed_max_loss, account_equity)
        if not ok:
            return EntryDecision(
                symbol, direction, True, rsi_v, why, cand, None,
                builder_report=report,
            )

        # 8. Submit (preflight is enforced inside place_multi_leg_order)
        order_id = str(uuid.uuid4())
        try:
            resp = self.public.place_multi_leg_order(
                legs=cand.legs,
                limit_price=cand.limit_price_str(),
                quantity=qty,
                order_id=order_id,
                time_in_force="DAY",
            )
        except PublicAPIError as exc:
            log.error("Order placement failed: %s", exc)
            self.notify.error(
                f"Order REJECTED — {symbol} {direction}",
                f"Spread: {cand.short_symbol} / {cand.long_symbol}\nError: {exc}",
            )
            return EntryDecision(
                symbol, direction, True, rsi_v, f"order failed: {exc}",
                cand, None, builder_report=report,
            )

        # 9. Persist as PENDING
        trade = TradeRecord(
            trade_id=order_id,
            underlying=symbol,
            direction=direction,
            short_strike=cand.short_strike,
            long_strike=cand.long_strike,
            width=cand.width,
            expiration=cand.expiration,
            short_symbol=cand.short_symbol,
            long_symbol=cand.long_symbol,
            quantity=qty,
            credit_received=cand.net_credit,
            open_order_id=resp.orderId,
            open_status="PENDING",
            opened_at=now_et.isoformat(),
            extra={
                "rsi_trigger": rsi_v,
                "short_delta": cand.short_delta,
                "credit_to_width": cand.credit_to_width,
                "mode": CONFIG.mode,
                "dry_run": self.public.dry_run,
            },
        )
        self.state.insert_trade(trade)

        self.notify.info(
            f"Order placed — {symbol} {direction} credit spread",
            f"{cand.short_symbol} (short) / {cand.long_symbol} (long)\n"
            f"Credit: ${cand.net_credit:.2f}  •  Width: ${cand.width:.2f}  •  "
            f"Qty: {qty}  •  Δ_short: {cand.short_delta:.2f}",
            fields=[
                {"name": "RSI(2)", "value": f"{rsi_v:.2f}" if rsi_v is not None else "n/a", "inline": True},
                {"name": "Mode", "value": CONFIG.mode, "inline": True},
                {"name": "Order ID", "value": resp.orderId[:16], "inline": True},
            ],
        )

        return EntryDecision(
            symbol, direction, True, rsi_v, None, cand, resp.orderId,
            builder_report=report,
        )

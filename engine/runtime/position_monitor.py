"""Position monitor — runs every minute during the session.

For each filled trade:
  1. Watch the open order until it FILLS / CANCELS / REJECTS.
  2. Once filled: place a GTC closing limit at 50% profit target.
     Closing credit-spread → BUY back spread for a debit ≤ 0.5 × credit.
  3. Watch for stop-loss conditions:
       a. Underlying touches/crosses the short strike → close at market-ish limit.
       b. Spread P&L worse than -2× credit → close.
       c. Time stop at 15:25 ET on the expiration day.
  4. When a closing fill is detected, record the close in StateStore and
     compute final P&L. Emit Discord notification.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

import pytz

from engine.broker.public_client import PublicAPIError, PublicClient
from engine.broker.spread_builder import closing_legs
from engine.broker.types import Instrument
from engine.notify.discord import Notifier
from engine.state.store import StateStore, TradeRecord
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)
ET = pytz.timezone("America/New_York")
TIME_STOP = time(15, 25)


def _to_float(s: Optional[str], default: float = 0.0) -> float:
    if s is None or s == "":
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


class PositionMonitor:
    def __init__(
        self,
        public_client: PublicClient,
        state: StateStore,
        notifier: Notifier,
    ) -> None:
        self.public = public_client
        self.state = state
        self.notify = notifier

    # ─────────────── pending → filled ─────────────────────────────────

    def reconcile_pending(self, now_et: datetime) -> None:
        """Pull order status for each PENDING trade; promote to FILLED or CANCELLED."""
        for t in self.state.pending_trades():
            if t.open_order_id is None:
                continue
            try:
                status = self.public.get_order_status(t.open_order_id)
            except PublicAPIError as exc:
                log.warning("get_order_status %s: %s", t.open_order_id, exc)
                continue

            s = status.status
            if s == "FILLED":
                avg = _to_float(status.averagePrice, t.credit_received)
                self.state.update_open_status(
                    t.trade_id,
                    open_status="FILLED",
                    opened_at=t.opened_at or now_et.isoformat(),
                    credit_received=avg if avg > 0 else t.credit_received,
                )
                log.info("Trade %s filled at %.2f credit", t.trade_id, avg)
                self.notify.success(
                    f"Filled — {t.underlying} {t.direction}",
                    f"{t.short_symbol} / {t.long_symbol}\n"
                    f"Credit: ${avg:.2f}  •  Qty: {t.quantity}",
                )
                # Immediately place the 50%-profit GTC close
                self._place_profit_target_close(self.state.get_trade(t.trade_id) or t)
            elif s in ("CANCELLED", "REJECTED", "EXPIRED"):
                self.state.update_open_status(t.trade_id, open_status=s)
                self.notify.warn(
                    f"Order {s.lower()} — {t.underlying} {t.direction}",
                    f"{t.short_symbol} / {t.long_symbol}\nReason: {status.rejectReason or 'n/a'}",
                )

    # ─────────────── 50% profit target ────────────────────────────────

    def _place_profit_target_close(self, t: TradeRecord) -> Optional[str]:
        """BUY-to-close at debit = (1 - profit_target_pct) × credit. GTC."""
        if t.credit_received <= 0:
            log.warning("No credit on %s — skipping profit target", t.trade_id)
            return None
        target_debit = round(t.credit_received * (1.0 - CONFIG.profit_target_pct), 2)
        target_debit = max(0.01, target_debit)
        legs = closing_legs(t.short_symbol, t.long_symbol)
        try:
            resp = self.public.place_multi_leg_order(
                legs=legs,
                limit_price=f"{target_debit:.2f}",
                quantity=t.quantity,
                time_in_force="GTC",
            )
        except PublicAPIError as exc:
            log.error("Profit target order failed for %s: %s", t.trade_id, exc)
            self.notify.error(
                f"Profit-target order failed — {t.underlying}",
                f"Trade {t.trade_id}: {exc}",
            )
            return None

        # Persist that we have an open close order. We don't mark close_status
        # FILLED yet — that happens on reconcile_close.
        self.state.conn.execute(
            "UPDATE trades SET close_order_id = ?, close_status = ? WHERE trade_id = ?",
            (resp.orderId, "WORKING_TARGET", t.trade_id),
        )
        self.state.conn.commit()
        log.info("Profit target placed: trade=%s order=%s debit=%.2f", t.trade_id, resp.orderId, target_debit)
        return resp.orderId

    # ─────────────── exit triggers ────────────────────────────────────

    def _underlying_quote(self, symbol: str) -> Optional[float]:
        try:
            q = self.public.get_quote(symbol, "EQUITY")
        except PublicAPIError as exc:
            log.warning("Quote for %s failed: %s", symbol, exc)
            return None
        last = _to_float(q.last)
        bid = _to_float(q.bid)
        ask = _to_float(q.ask)
        if last > 0:
            return last
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None

    def _spread_mid(self, t: TradeRecord) -> Optional[float]:
        """Current MID of the spread = short_mid - long_mid (≥0 = our liability to buy back)."""
        try:
            r = self.public.get_quotes(
                [
                    Instrument(symbol=t.short_symbol, type="OPTION"),
                    Instrument(symbol=t.long_symbol, type="OPTION"),
                ]
            )
        except PublicAPIError as exc:
            log.warning("Spread mid quote failed for %s: %s", t.trade_id, exc)
            return None
        if len(r.quotes) != 2:
            return None
        s_q, l_q = r.quotes[0], r.quotes[1]
        s_mid = (_to_float(s_q.bid) + _to_float(s_q.ask)) / 2.0
        l_mid = (_to_float(l_q.bid) + _to_float(l_q.ask)) / 2.0
        return round(s_mid - l_mid, 2)

    def _hard_close(
        self, t: TradeRecord, *, reason: str, limit_price: float, now_et: datetime
    ) -> None:
        """Cancel any working close order, then submit an aggressive market-ish limit close."""
        # Cancel pending profit target if any
        if t.close_order_id:
            try:
                self.public.cancel_order(t.close_order_id)
            except PublicAPIError as exc:
                log.warning("Cancel of close order %s failed: %s", t.close_order_id, exc)

        legs = closing_legs(t.short_symbol, t.long_symbol)
        # Bound the debit we'll pay at width (the theoretical max-loss debit)
        bounded = max(0.01, min(t.width, limit_price))
        try:
            resp = self.public.place_multi_leg_order(
                legs=legs,
                limit_price=f"{bounded:.2f}",
                quantity=t.quantity,
                time_in_force="DAY",
            )
        except PublicAPIError as exc:
            log.error("Hard close failed for %s: %s", t.trade_id, exc)
            self.notify.error(
                f"Hard-close FAILED — {t.underlying}",
                f"Trade {t.trade_id} reason={reason}: {exc}",
            )
            return

        self.state.conn.execute(
            "UPDATE trades SET close_order_id = ?, close_status = ?, close_reason = ? WHERE trade_id = ?",
            (resp.orderId, "WORKING_STOP", reason, t.trade_id),
        )
        self.state.conn.commit()
        log.warning(
            "Hard close placed: trade=%s reason=%s debit=%.2f",
            t.trade_id, reason, bounded,
        )
        self.notify.warn(
            f"Closing position — {t.underlying} ({reason})",
            f"{t.short_symbol} / {t.long_symbol}\nLimit debit: ${bounded:.2f}  •  Qty: {t.quantity}",
        )

    def check_exits(self, now_et: datetime) -> None:
        """For each filled trade, check tested-strike, stop-loss, time-stop."""
        for t in self.state.open_trades():
            # Tested strike
            spot = self._underlying_quote(t.underlying)
            if spot is not None:
                if t.direction == "PUT" and spot <= t.short_strike:
                    self._hard_close(t, reason="TESTED_STRIKE", limit_price=t.width, now_et=now_et)
                    continue
                if t.direction == "CALL" and spot >= t.short_strike:
                    self._hard_close(t, reason="TESTED_STRIKE", limit_price=t.width, now_et=now_et)
                    continue

            # 2× credit stop (only relevant after we have a meaningful credit)
            mid = self._spread_mid(t)
            if mid is not None and t.credit_received > 0:
                # Loss happens when the close-debit exceeds (1 + stop_loss_multiple) × credit
                # ... wait: P&L per spread = credit - debit. Loss = -2× credit means debit = 3× credit.
                # We close at debit ≥ (1 + stop_loss_multiple) × credit.
                stop_debit = (1.0 + CONFIG.stop_loss_multiple) * t.credit_received
                if mid >= stop_debit:
                    self._hard_close(t, reason="STOP_LOSS", limit_price=mid, now_et=now_et)
                    continue

            # Time stop on expiration day at 15:25 ET
            try:
                exp_date = date.fromisoformat(t.expiration)
            except Exception:  # noqa: BLE001
                exp_date = None
            if (
                exp_date is not None
                and now_et.date() == exp_date
                and now_et.time() >= TIME_STOP
            ):
                # Pay up to width to flatten before Public's 15:30 auto-cancel
                self._hard_close(t, reason="TIME_STOP", limit_price=t.width, now_et=now_et)

    # ─────────────── close reconciliation ─────────────────────────────

    def reconcile_closes(self, now_et: datetime) -> None:
        """Pull status of working close orders. On FILLED → record final P&L."""
        # Find trades that have a close_order_id but pnl is still null
        rows = self.state.conn.execute(
            """
            SELECT trade_id, close_order_id, close_reason FROM trades
            WHERE close_order_id IS NOT NULL AND pnl IS NULL
            """
        ).fetchall()
        for r in rows:
            try:
                status = self.public.get_order_status(r["close_order_id"])
            except PublicAPIError as exc:
                log.warning("get_order_status close %s: %s", r["close_order_id"], exc)
                continue

            s = status.status
            if s == "FILLED":
                debit = _to_float(status.averagePrice, 0.0)
                reason = r["close_reason"] or "PROFIT_TARGET"
                # If we never set a reason, infer from working state name
                # (WORKING_TARGET → PROFIT_TARGET; WORKING_STOP → preserved above)
                updated = self.state.record_close(
                    r["trade_id"],
                    close_order_id=r["close_order_id"],
                    close_status="FILLED",
                    closed_at=now_et.isoformat(),
                    close_reason=reason,
                    debit_paid=debit,
                )
                if updated:
                    color_method = self.notify.success if (updated.pnl or 0) >= 0 else self.notify.warn
                    color_method(
                        f"Closed — {updated.underlying} {updated.direction} ({reason})",
                        f"{updated.short_symbol} / {updated.long_symbol}\n"
                        f"Credit: ${updated.credit_received:.2f}  •  Debit: ${debit:.2f}\n"
                        f"P&L: **${updated.pnl:+,.2f}**  •  Qty: {updated.quantity}",
                    )
            elif s in ("CANCELLED", "REJECTED", "EXPIRED"):
                # A target close that got cancelled goes back to "open" so we can re-place
                if r["close_reason"] in (None, "PROFIT_TARGET"):
                    self.state.conn.execute(
                        "UPDATE trades SET close_order_id = NULL, close_status = NULL WHERE trade_id = ?",
                        (r["trade_id"],),
                    )
                    self.state.conn.commit()
                    log.info("Close order %s ended in %s — clearing", r["close_order_id"], s)

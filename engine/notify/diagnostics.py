"""Diagnostic reporter — posts gate-evaluation telemetry to a separate Discord channel.

The main #harvest channel only sees morning context, trade orders, fills, exits,
and EOD summaries. This reporter is for the verbose stuff:

  * Periodic snapshots of every gate's state during the entry window
  * "Near-miss" alerts when a symbol almost qualifies (RSI just outside threshold
    with IV gate passing) — useful during DRY_RUN to see which knob is the
    bottleneck on no-trade days

If `DISCORD_WEBHOOK_DIAGNOSTICS_URL` is not set, the reporter is a no-op so the
engine can run without it.

Failures in here MUST NEVER block trading logic — every send is wrapped and
swallowed, same as the main notifier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from engine.notify.discord import (
    COLOR_BLUE,
    COLOR_GREY,
    COLOR_YELLOW,
    DiscordNotifier,
    NullNotifier,
    Notifier,
)
from engine.runtime.entry_detector import EntryDecision
from engine.runtime.market_context import MarketContext
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class GateEvalRow:
    """One row of the per-symbol gate evaluation table."""
    symbol: str
    direction: Optional[str]
    regime: str
    iv_gate_passed: Optional[bool]
    ivr: Optional[float]
    ivp: Optional[float]
    rsi_value: Optional[float]
    rsi_threshold: Optional[float]
    triggered: bool
    blocked_reason: Optional[str]
    placed: bool

    def status_str(self) -> str:
        if self.placed:
            return "✅ PLACED"
        if self.triggered:
            return f"⚠ TRIGGERED but {self.blocked_reason or '?'}"
        if self.blocked_reason:
            short = self.blocked_reason
            if len(short) > 50:
                short = short[:47] + "..."
            return short
        return "—"


def near_miss(row: GateEvalRow, band: float) -> bool:
    """True if the symbol almost qualified — IV gate passing but RSI just
    outside the trigger threshold by `band` points or less.
    """
    if row.iv_gate_passed is not True:
        return False
    if row.rsi_value is None or row.rsi_threshold is None:
        return False
    if row.direction == "PUT":
        # Need RSI < threshold (e.g. 10). Near miss = within band above it.
        gap = row.rsi_value - row.rsi_threshold
        return 0 <= gap <= band
    if row.direction == "CALL":
        # Need RSI > threshold (e.g. 90). Near miss = within band below it.
        gap = row.rsi_threshold - row.rsi_value
        return 0 <= gap <= band
    return False


def build_row(symbol: str, market: MarketContext, decision: EntryDecision) -> GateEvalRow:
    """Pull all the gate state for one symbol into a single row."""
    ctx = market.underlyings.get(symbol)
    if ctx is None:
        return GateEvalRow(
            symbol=symbol, direction=None, regime="?",
            iv_gate_passed=None, ivr=None, ivp=None,
            rsi_value=decision.rsi_value, rsi_threshold=None,
            triggered=decision.triggered,
            blocked_reason=decision.blocked_reason,
            placed=bool(decision.placed_order_id),
        )
    direction = decision.direction
    rsi_threshold: Optional[float]
    if direction == "PUT":
        rsi_threshold = CONFIG.rsi_oversold
    elif direction == "CALL":
        rsi_threshold = CONFIG.rsi_overbought
    else:
        rsi_threshold = None
    return GateEvalRow(
        symbol=symbol,
        direction=direction,
        regime=ctx.regime.regime.value,
        iv_gate_passed=ctx.iv_gate_passed,
        ivr=ctx.iv.ivr,
        ivp=ctx.iv.ivp,
        rsi_value=decision.rsi_value,
        rsi_threshold=rsi_threshold,
        triggered=decision.triggered,
        blocked_reason=decision.blocked_reason,
        placed=bool(decision.placed_order_id),
    )


def build_diagnostics_notifier(webhook_url: Optional[str] = None) -> Notifier:
    """Factory — returns DiscordNotifier (named 'Harvest Diagnostics') if
    a webhook is configured, else NullNotifier.
    """
    url = webhook_url if webhook_url is not None else CONFIG.discord_diagnostics_webhook_url
    if url:
        return DiscordNotifier(url, username="Harvest Diagnostics")
    log.info("No diagnostics webhook configured — diagnostics will not be posted")
    return NullNotifier()


class DiagnosticsReporter:
    """Stateful reporter — handles rate-limiting of snapshot pings."""

    def __init__(
        self,
        notifier: Optional[Notifier] = None,
        snapshot_interval_s: Optional[int] = None,
        near_miss_band: Optional[float] = None,
    ) -> None:
        self.notify = notifier or build_diagnostics_notifier()
        self.snapshot_interval = timedelta(
            seconds=snapshot_interval_s
            if snapshot_interval_s is not None
            else CONFIG.diagnostics_snapshot_interval_s
        )
        self.near_miss_band = (
            near_miss_band
            if near_miss_band is not None
            else CONFIG.diagnostics_near_miss_rsi_band
        )
        self._last_snapshot_at: Optional[datetime] = None
        # Per (symbol, date) — only ping near-miss once per day per symbol.
        self._near_miss_pinged_today: set[tuple[str, str]] = set()

    # ─────────── snapshot ───────────────────────────────────────────────

    def maybe_post_snapshot(self, now_et: datetime, rows: list[GateEvalRow]) -> bool:
        """Post a gate-eval snapshot table if enough time has passed.

        Returns True if posted, False if rate-limited or empty.
        """
        if not rows:
            return False
        if self._last_snapshot_at is not None:
            if now_et - self._last_snapshot_at < self.snapshot_interval:
                return False
        self._last_snapshot_at = now_et
        return self._post_snapshot(now_et, rows)

    def _post_snapshot(self, now_et: datetime, rows: list[GateEvalRow]) -> bool:
        title = f"Gate eval — {now_et.strftime('%H:%M ET')}"
        # Build a code-block table so it lines up nicely in Discord
        lines = [
            "```",
            f"{'Sym':<4} {'Dir':<4} {'Regime':<6} {'IV gate':<10} {'RSI(2)':<8} {'Status'}",
            "─" * 72,
        ]
        for r in rows:
            iv_cell = "—"
            if r.iv_gate_passed is True:
                iv_cell = f"✓ IVR{r.ivr:.0f}" if r.ivr is not None else "✓"
            elif r.iv_gate_passed is False:
                iv_cell = f"✗ IVR{r.ivr:.0f}" if r.ivr is not None else "✗"
            rsi_cell = f"{r.rsi_value:.1f}" if r.rsi_value is not None else "—"
            dir_cell = r.direction or "—"
            lines.append(
                f"{r.symbol:<4} {dir_cell:<4} {r.regime:<6} {iv_cell:<10} {rsi_cell:<8} {r.status_str()}"
            )
        lines.append("```")
        body = "\n".join(lines)
        return self.notify.send(title, body, color=COLOR_GREY)

    # ─────────── near-miss alerts ──────────────────────────────────────

    def maybe_post_near_misses(self, now_et: datetime, rows: list[GateEvalRow]) -> int:
        """Send a near-miss embed for any symbol that passed IV but missed
        the RSI trigger by ≤ `near_miss_band`. Each (symbol, day) only pings
        once.

        Returns the number of pings sent.
        """
        sent = 0
        day_key = now_et.strftime("%Y-%m-%d")
        for r in rows:
            if not near_miss(r, self.near_miss_band):
                continue
            key = (r.symbol, day_key)
            if key in self._near_miss_pinged_today:
                continue
            self._near_miss_pinged_today.add(key)
            sent += int(self._post_near_miss(now_et, r))
        return sent

    def _post_near_miss(self, now_et: datetime, row: GateEvalRow) -> bool:
        title = f"⚠ Near-miss — {row.symbol} {row.direction or ''}".strip()
        gap = abs((row.rsi_value or 0) - (row.rsi_threshold or 0))
        op = "<" if row.direction == "PUT" else ">"
        body = (
            f"RSI(2) = **{row.rsi_value:.2f}** "
            f"(need {op} {row.rsi_threshold:.0f}, missed by {gap:.2f})\n"
            f"IV gate: ✓ (IVR={row.ivr:.1f} IVP={row.ivp:.1f})\n"
            f"Regime: {row.regime}\n"
            f"*No trade — RSI just outside threshold*"
        )
        return self.notify.send(title, body, color=COLOR_YELLOW)

    # ─────────── EOD ────────────────────────────────────────────────────

    def post_eod_summary(
        self,
        now_et: datetime,
        gate_pass_counts: dict[str, int],
        triggered_count: int,
        placed_count: int,
        evaluations: int,
    ) -> bool:
        """End-of-day diagnostic summary: how many evaluations, how many got
        through which gates, how many fired."""
        title = f"Diagnostics EOD — {now_et.strftime('%Y-%m-%d')}"
        lines = [
            f"Total evaluations across the entry window: **{evaluations}**",
            "",
            f"Passed regime gate: **{gate_pass_counts.get('regime', 0)}**",
            f"Passed VIX/event gate: **{gate_pass_counts.get('macro', 0)}**",
            f"Passed IV gate: **{gate_pass_counts.get('iv', 0)}**",
            f"RSI triggered: **{triggered_count}**",
            f"Orders placed: **{placed_count}**",
        ]
        return self.notify.send(title, "\n".join(lines), color=COLOR_BLUE)


__all__ = [
    "DiagnosticsReporter",
    "GateEvalRow",
    "build_diagnostics_notifier",
    "build_row",
    "near_miss",
]

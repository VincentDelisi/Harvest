"""Kill switch — global halt with manual reset only.

Triggered by any of:
  • Day P&L ≤ −kill_switch_daily_loss_pct of starting equity
  • kill_switch_consecutive_losses losses in a row (across days)
  • VIX ≥ vix_max
  • Repeated API errors (caller increments via record_api_error)

Once triggered, EntryDetector must check `is_active()` BEFORE placing any order
and abort. Existing positions are still managed by PositionMonitor — kill switch
only stops new entries. Manual reset is required to resume.
"""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

from engine.notify.discord import Notifier
from engine.state.store import StateStore
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)
ET = pytz.timezone("America/New_York")


class KillSwitch:
    def __init__(
        self,
        state: StateStore,
        notifier: Notifier,
        api_error_window_seconds: int = 300,
        api_error_threshold: int = 5,
    ) -> None:
        self.state = state
        self.notify = notifier
        self._api_errors: deque[datetime] = deque()
        self._window = timedelta(seconds=api_error_window_seconds)
        self._threshold = api_error_threshold

    def is_active(self) -> bool:
        return self.state.get_kill_switch().active

    def reason(self) -> Optional[str]:
        return self.state.get_kill_switch().reason

    def trigger(self, reason: str) -> None:
        if self.is_active():
            return  # already halted
        self.state.trigger_kill_switch(reason)
        self.notify.error(
            "KILL SWITCH activated",
            f"{reason}\n\n**No new entries will be taken.** Existing positions are still managed.\n\nManual reset required.",
        )

    def reset(self) -> None:
        self.state.reset_kill_switch()
        self.notify.warn(
            "Kill switch reset",
            "Engine cleared to take new entries again.",
        )

    # ─────────────── runtime checks ──────────────────────────────────

    def evaluate(
        self,
        *,
        starting_equity: Optional[float],
        current_vix: Optional[float],
        now_et: Optional[datetime] = None,
    ) -> bool:
        """Run all halt-checks. Returns True if the kill switch is now active."""
        if self.is_active():
            return True
        now_et = now_et or datetime.now(ET)

        # 1. Day P&L drawdown
        if starting_equity is not None and starting_equity > 0:
            day_pnl = self.state.realized_pnl_on(now_et.date())
            dd = -day_pnl / starting_equity if day_pnl < 0 else 0.0
            if dd >= CONFIG.kill_switch_daily_loss_pct:
                self.trigger(
                    f"Daily drawdown {dd*100:.1f}% ≥ "
                    f"{CONFIG.kill_switch_daily_loss_pct*100:.1f}% (P&L=${day_pnl:.2f}, equity=${starting_equity:.2f})"
                )
                return True

        # 2. Consecutive losses
        streak = self.state.consecutive_losses()
        if streak >= CONFIG.kill_switch_consecutive_losses:
            self.trigger(f"{streak} consecutive losing trades")
            return True

        # 3. VIX cap
        if current_vix is not None and current_vix >= CONFIG.vix_max:
            self.trigger(f"VIX={current_vix:.2f} ≥ {CONFIG.vix_max}")
            return True

        # 4. API error storm
        self._prune_api_errors(now_et)
        if len(self._api_errors) >= self._threshold:
            self.trigger(
                f"{len(self._api_errors)} API errors in last {self._window.total_seconds():.0f}s"
            )
            return True

        return False

    def record_api_error(self, when: Optional[datetime] = None) -> None:
        self._api_errors.append(when or datetime.now(ET))

    def _prune_api_errors(self, now_et: datetime) -> None:
        cutoff = now_et - self._window
        while self._api_errors and self._api_errors[0] < cutoff:
            self._api_errors.popleft()

"""Event blackout calendar per STRATEGY_SPEC.md §7.

Loads FOMC / CPI / NFP / Powell speech dates from config/event_calendar.yaml.
Returns whether today (or the prior PM session) is blacked out for new entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

import pandas_market_calendars as mcal
import pytz
import yaml

from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)

ET = pytz.timezone("America/New_York")
PRIOR_SESSION_CUTOFF = time(14, 0)  # No new entries after 14:00 ET on prior session


@dataclass
class BlackoutResult:
    blocked: bool
    reason: str | None
    next_clear_at: datetime | None


class EventCalendar:
    def __init__(self, yaml_path: str | None = None) -> None:
        path = Path(yaml_path) if yaml_path else Path(__file__).resolve().parents[2] / "config" / "event_calendar.yaml"
        with open(path, "r") as fh:
            data = yaml.safe_load(fh) or {}

        self._dates: set[date] = set()
        self._reasons: dict[date, str] = {}
        for category, label in [
            ("fomc_meetings", "FOMC"),
            ("cpi_releases", "CPI"),
            ("nfp_releases", "NFP"),
            ("powell_speeches", "Powell"),
            ("manual_blackouts", "Manual"),
        ]:
            for d in data.get(category) or []:
                d_obj = d if isinstance(d, date) else date.fromisoformat(str(d))
                self._dates.add(d_obj)
                self._reasons[d_obj] = label

        self._nyse = mcal.get_calendar("NYSE")
        log.info("Loaded %d blackout dates from %s", len(self._dates), path)

    def is_event_day(self, on: date) -> tuple[bool, str | None]:
        return (on in self._dates), self._reasons.get(on)

    def prior_trading_day(self, on: date) -> date:
        """Most recent NYSE trading day strictly before `on`."""
        sched = self._nyse.schedule(start_date=on - timedelta(days=10), end_date=on)
        sessions = [d.date() for d in sched.index]
        prior = [d for d in sessions if d < on]
        return prior[-1] if prior else on - timedelta(days=1)

    def check(self, now_et: datetime | None = None) -> BlackoutResult:
        """Return whether new entries are blocked at this moment."""
        if now_et is None:
            now_et = datetime.now(ET)
        elif now_et.tzinfo is None:
            now_et = ET.localize(now_et)

        today = now_et.date()

        # Today is an event day → blocked all day
        is_event, reason = self.is_event_day(today)
        if is_event:
            return BlackoutResult(
                blocked=True,
                reason=f"{reason} event day",
                next_clear_at=ET.localize(
                    datetime.combine(today + timedelta(days=1), time(9, 30))
                ),
            )

        # If next trading day is an event day, check prior-session cutoff
        next_event = self._next_event_within(today, days=5)
        if next_event is not None:
            next_event_date, next_event_reason = next_event
            prior_session = self.prior_trading_day(next_event_date)
            if today == prior_session and now_et.time() >= PRIOR_SESSION_CUTOFF:
                return BlackoutResult(
                    blocked=True,
                    reason=f"Prior session lockout for {next_event_reason} on {next_event_date}",
                    next_clear_at=ET.localize(
                        datetime.combine(next_event_date + timedelta(days=1), time(9, 30))
                    ),
                )

        return BlackoutResult(blocked=False, reason=None, next_clear_at=None)

    def _next_event_within(
        self, start: date, days: int
    ) -> tuple[date, str] | None:
        for offset in range(1, days + 1):
            d = start + timedelta(days=offset)
            if d in self._dates:
                return d, self._reasons[d]
        return None

    def all_dates(self) -> Iterable[date]:
        return sorted(self._dates)

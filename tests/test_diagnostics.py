"""Tests for engine.notify.diagnostics — gate-eval reporter."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytz

from engine.notify.diagnostics import (
    DiagnosticsReporter,
    GateEvalRow,
    near_miss,
)
from engine.runtime.entry_detector import EntryDecision

ET = pytz.timezone("America/New_York")


class FakeNotifier:
    """Captures sends so we can assert on them."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []

    def send(self, title: str, description: str = "", *, color: int = 0,
             fields: list | None = None) -> bool:
        self.sends.append({"title": title, "description": description,
                           "color": color, "fields": fields})
        return True

    def info(self, t, d="", **kw): return self.send(t, d, color=0)
    def success(self, t, d="", **kw): return self.send(t, d, color=0)
    def warn(self, t, d="", **kw): return self.send(t, d, color=0)
    def error(self, t, d="", **kw): return self.send(t, d, color=0)


def _row(symbol="QQQ", direction="PUT", iv=True, ivr=22.0, ivp=49.0,
         rsi=12.0, threshold=10.0, triggered=False, blocked="no RSI trigger",
         placed=False, regime="BULL") -> GateEvalRow:
    return GateEvalRow(
        symbol=symbol, direction=direction, regime=regime,
        iv_gate_passed=iv, ivr=ivr, ivp=ivp,
        rsi_value=rsi, rsi_threshold=threshold,
        triggered=triggered, blocked_reason=blocked, placed=placed,
    )


# ─────────────────── near_miss logic ──────────────────────────────────


def test_near_miss_put_just_above_threshold_returns_true():
    # PUT needs RSI<10. Got 12. With band=5, that's a near-miss.
    row = _row(direction="PUT", rsi=12.0, threshold=10.0, iv=True)
    assert near_miss(row, band=5.0) is True


def test_near_miss_put_far_above_threshold_returns_false():
    # PUT needs RSI<10. Got 30. Even with band=5, that's not close.
    row = _row(direction="PUT", rsi=30.0, threshold=10.0, iv=True)
    assert near_miss(row, band=5.0) is False


def test_near_miss_put_already_below_threshold_returns_false():
    # If RSI is already below threshold the trigger fired (or should have);
    # this isn't a "miss".
    row = _row(direction="PUT", rsi=8.0, threshold=10.0, iv=True)
    assert near_miss(row, band=5.0) is False


def test_near_miss_call_just_below_threshold_returns_true():
    # CALL needs RSI>90. Got 87. With band=5, near-miss.
    row = _row(direction="CALL", rsi=87.0, threshold=90.0, iv=True)
    assert near_miss(row, band=5.0) is True


def test_near_miss_iv_gate_failed_returns_false():
    # Even if RSI is close, if the IV gate failed, it's not a near-miss
    # (we'd have been blocked anyway).
    row = _row(iv=False, rsi=12.0, threshold=10.0)
    assert near_miss(row, band=5.0) is False


def test_near_miss_no_rsi_value_returns_false():
    row = _row(rsi=None, threshold=10.0)
    assert near_miss(row, band=5.0) is False


def test_near_miss_no_direction_returns_false():
    row = _row(direction=None, rsi=12.0, threshold=10.0)
    assert near_miss(row, band=5.0) is False


# ─────────────────── snapshot rate-limiting ───────────────────────────


def test_first_snapshot_posts():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake, snapshot_interval_s=300)
    now = ET.localize(datetime(2026, 5, 8, 10, 30))
    rep.maybe_post_snapshot(now, [_row()])
    assert len(fake.sends) == 1
    assert "Gate eval" in fake.sends[0]["title"]


def test_second_snapshot_within_window_is_suppressed():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake, snapshot_interval_s=300)
    now = ET.localize(datetime(2026, 5, 8, 10, 30))
    rep.maybe_post_snapshot(now, [_row()])
    rep.maybe_post_snapshot(now + timedelta(seconds=120), [_row()])
    assert len(fake.sends) == 1


def test_snapshot_after_interval_posts_again():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake, snapshot_interval_s=300)
    now = ET.localize(datetime(2026, 5, 8, 10, 30))
    rep.maybe_post_snapshot(now, [_row()])
    rep.maybe_post_snapshot(now + timedelta(seconds=301), [_row()])
    assert len(fake.sends) == 2


def test_snapshot_with_empty_rows_is_skipped():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake, snapshot_interval_s=1)
    now = ET.localize(datetime(2026, 5, 8, 10, 30))
    posted = rep.maybe_post_snapshot(now, [])
    assert posted is False
    assert fake.sends == []


# ─────────────────── near-miss dedupe per day ─────────────────────────


def test_near_miss_pings_once_per_day_per_symbol():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake, snapshot_interval_s=999,
                              near_miss_band=5.0)
    now = ET.localize(datetime(2026, 5, 8, 10, 30))
    row = _row(symbol="QQQ", rsi=12.0, threshold=10.0, iv=True)
    n1 = rep.maybe_post_near_misses(now, [row])
    n2 = rep.maybe_post_near_misses(now + timedelta(minutes=10), [row])
    assert n1 == 1
    assert n2 == 0
    assert len(fake.sends) == 1


def test_near_miss_pings_again_next_day():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake, snapshot_interval_s=999,
                              near_miss_band=5.0)
    day1 = ET.localize(datetime(2026, 5, 8, 10, 30))
    day2 = ET.localize(datetime(2026, 5, 9, 10, 30))
    row = _row(symbol="QQQ", rsi=12.0, threshold=10.0, iv=True)
    rep.maybe_post_near_misses(day1, [row])
    rep.maybe_post_near_misses(day2, [row])
    assert len(fake.sends) == 2


def test_near_miss_distinct_symbols_each_ping():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake, snapshot_interval_s=999,
                              near_miss_band=5.0)
    now = ET.localize(datetime(2026, 5, 8, 10, 30))
    rows = [
        _row(symbol="QQQ", rsi=12.0, threshold=10.0, iv=True),
        _row(symbol="SPY", rsi=13.0, threshold=10.0, iv=True),
    ]
    rep.maybe_post_near_misses(now, rows)
    assert len(fake.sends) == 2


# ─────────────────── EOD summary ──────────────────────────────────────


def test_eod_summary_posts():
    fake = FakeNotifier()
    rep = DiagnosticsReporter(notifier=fake)
    now = ET.localize(datetime(2026, 5, 8, 16, 0))
    rep.post_eod_summary(
        now,
        gate_pass_counts={"regime": 12, "macro": 18, "iv": 6},
        triggered_count=2,
        placed_count=1,
        evaluations=18,
    )
    assert len(fake.sends) == 1
    body = fake.sends[0]["description"]
    assert "evaluations" in body.lower()
    assert "18" in body

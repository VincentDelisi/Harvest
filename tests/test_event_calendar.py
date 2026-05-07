"""Event blackout calendar tests."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, time
from pathlib import Path

import pytz
import yaml

from engine.risk.event_calendar import EventCalendar

ET = pytz.timezone("America/New_York")


def _write_calendar(tmp_path: Path, **kwargs) -> Path:
    data = {
        "fomc_meetings": kwargs.get("fomc", []),
        "cpi_releases": kwargs.get("cpi", []),
        "nfp_releases": kwargs.get("nfp", []),
        "powell_speeches": kwargs.get("powell", []),
        "manual_blackouts": kwargs.get("manual", []),
    }
    p = tmp_path / "events.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_fomc_day_blocked_all_day():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_calendar(Path(tmp), fomc=["2026-06-17"])
        cal = EventCalendar(yaml_path=str(path))
        # Wednesday June 17 at 10:30 ET
        result = cal.check(now_et=datetime(2026, 6, 17, 10, 30))
        assert result.blocked is True
        assert "FOMC" in result.reason


def test_prior_session_morning_not_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        # FOMC is Wed Jun 17 → prior session is Tue Jun 16
        path = _write_calendar(Path(tmp), fomc=["2026-06-17"])
        cal = EventCalendar(yaml_path=str(path))
        # Tue Jun 16 at 10:00 ET — before 14:00 cutoff → allowed
        result = cal.check(now_et=datetime(2026, 6, 16, 10, 0))
        assert result.blocked is False


def test_prior_session_afternoon_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_calendar(Path(tmp), fomc=["2026-06-17"])
        cal = EventCalendar(yaml_path=str(path))
        # Tue Jun 16 at 14:30 ET — after cutoff → blocked
        result = cal.check(now_et=datetime(2026, 6, 16, 14, 30))
        assert result.blocked is True
        assert "Prior session" in result.reason


def test_normal_day_not_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_calendar(Path(tmp), fomc=["2026-06-17"])
        cal = EventCalendar(yaml_path=str(path))
        # Random Wednesday with no nearby events
        result = cal.check(now_et=datetime(2026, 5, 13, 10, 30))
        assert result.blocked is False


def test_cpi_day_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_calendar(Path(tmp), cpi=["2026-05-13"])
        cal = EventCalendar(yaml_path=str(path))
        result = cal.check(now_et=datetime(2026, 5, 13, 11, 0))
        assert result.blocked is True
        assert "CPI" in result.reason

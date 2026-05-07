"""Daily market context — regime, IV stats, VIX, event blackouts.

Built once per trading day in the morning routine and consumed by the
EntryDetector. All gates (event, regime, VIX, IV) are evaluated here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd
import pytz

from engine.data.polygon_rest import PolygonREST
from engine.risk.event_calendar import EventCalendar
from engine.strategy.iv_engine import IVEngine, IVStats
from engine.strategy.regime import Regime, RegimeSnapshot, classify
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class UnderlyingContext:
    symbol: str
    regime: RegimeSnapshot
    iv: IVStats
    vix_proxy_value: float
    iv_gate_passed: bool

    @property
    def trades_puts(self) -> bool:
        return self.regime.trades_puts

    @property
    def trades_calls(self) -> bool:
        return self.regime.trades_calls


@dataclass
class MarketContext:
    as_of: datetime
    blackout_active: bool
    blackout_reason: Optional[str]
    vix_value: float                # SPY's VIX
    vix_gate_passed: bool           # VIX < vix_max
    underlyings: dict[str, UnderlyingContext]

    def is_tradeable(self, symbol: str, direction: str) -> tuple[bool, str]:
        """Combined entry gate. Returns (allowed, reason_if_not)."""
        if self.blackout_active:
            return False, f"event blackout: {self.blackout_reason}"
        if not self.vix_gate_passed:
            return False, f"VIX gate failed ({self.vix_value:.2f} >= {CONFIG.vix_max})"
        ctx = self.underlyings.get(symbol)
        if ctx is None:
            return False, f"no context loaded for {symbol}"
        if ctx.regime.regime == Regime.MIXED:
            return False, f"{symbol} regime MIXED — no trades"
        if direction == "PUT" and not ctx.trades_puts:
            return False, f"{symbol} regime is {ctx.regime.regime.value}, doesn't trade puts"
        if direction == "CALL" and not ctx.trades_calls:
            return False, f"{symbol} regime is {ctx.regime.regime.value}, doesn't trade calls"
        if not ctx.iv_gate_passed:
            return (
                False,
                f"{symbol} IV gate failed (IVR={ctx.iv.ivr:.1f} IVP={ctx.iv.ivp:.1f})",
            )
        return True, "OK"


class MarketContextBuilder:
    """Builds the daily MarketContext from Polygon + IV history."""

    def __init__(
        self,
        polygon: PolygonREST,
        iv_engine: IVEngine,
        event_calendar: EventCalendar,
    ) -> None:
        self.polygon = polygon
        self.iv = iv_engine
        self.events = event_calendar

    def build(self, today_iv_by_symbol: dict[str, float], now_et: Optional[datetime] = None) -> MarketContext:
        """Build the daily context.

        Args:
            today_iv_by_symbol: today's ATM IV per underlying, fetched from
                the Public option chain at ~30 DTE before this call.
            now_et: override clock (mostly for tests)
        """
        if now_et is None:
            now_et = datetime.now(ET)
        elif now_et.tzinfo is None:
            now_et = ET.localize(now_et)

        # 1. Event blackout
        bo = self.events.check(now_et)
        log.info("Blackout check: blocked=%s reason=%s", bo.blocked, bo.reason)

        # 2. VIX (used both as macro gate and as bootstrap for SPY IVR/IVP)
        vix_value = self.polygon.latest_index_value("VIX") or 0.0
        vix_gate_passed = vix_value < CONFIG.vix_max
        log.info("VIX=%.2f gate_passed=%s", vix_value, vix_gate_passed)

        # 3. Per-underlying regime + IV
        underlyings: dict[str, UnderlyingContext] = {}
        for sym in CONFIG.underlyings:
            # Daily bars for SMA/regime
            daily = self.polygon.daily_bars(sym, lookback_days=300)
            regime = classify(sym, daily)
            log.info(
                "%s regime=%s close=%.2f sma50=%.2f sma200=%.2f",
                sym, regime.regime.value, regime.close, regime.sma50, regime.sma200,
            )

            # IV gate — bootstrap from VIX/VXN/RVX until we have 252 own days
            proxy_ticker = CONFIG.vix_proxies.get(sym, "VIX")
            proxy_value = self.polygon.latest_index_value(proxy_ticker) or 0.0
            today_iv = today_iv_by_symbol.get(sym)
            if today_iv is None:
                log.warning("No today_iv for %s — skipping", sym)
                continue
            # Persist today's reading
            self.iv.log_atm_iv(sym, now_et.date(), today_iv)
            # Build a bootstrap series from the proxy's daily history
            bootstrap_df = self.polygon.daily_bars(f"I:{proxy_ticker}", lookback_days=300)
            bootstrap_series = bootstrap_df["close"] if not bootstrap_df.empty else None
            stats = self.iv.compute_stats(sym, today_iv, bootstrap_series)
            iv_gate_passed = IVEngine.passes_volatility_gate(stats)
            log.info(
                "%s IVR=%.1f IVP=%.1f bootstrap=%s gate_passed=%s",
                sym, stats.ivr, stats.ivp, stats.bootstrap, iv_gate_passed,
            )

            underlyings[sym] = UnderlyingContext(
                symbol=sym,
                regime=regime,
                iv=stats,
                vix_proxy_value=proxy_value,
                iv_gate_passed=iv_gate_passed,
            )

        return MarketContext(
            as_of=now_et,
            blackout_active=bo.blocked,
            blackout_reason=bo.reason,
            vix_value=vix_value,
            vix_gate_passed=vix_gate_passed,
            underlyings=underlyings,
        )

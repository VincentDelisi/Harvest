"""Main strategy engine — the daily trading loop.

State machine (UTC America/New_York):

  PRE_OPEN  → 09:00 ET  : load market context (regime, IV, blackouts)
  OPEN      → 09:30 ET  : market open, no entries yet
  ENTRY     → 10:00 ET  : entry window starts; tick every 30s
  MID_DAY   → 11:30 ET  : entry window closes; only monitor positions
  WIND_DOWN → 15:25 ET  : time-stop hard-closes anything expiring today
  CLOSE     → 16:00 ET  : log day P&L; sleep until next trading day

Modes:
  DRY_RUN     — preflight only, no orders submitted, dry_run=True everywhere
  LIVE_SMALL  — real orders, capped at 1 contract, max 30 trades
  LIVE        — full sizing per spec

This is a synchronous polling loop. It deliberately doesn't use asyncio —
the cadence is leisurely (30–60 s ticks) and the ergonomics of plain Python
won out for clarity and testability.
"""
from __future__ import annotations

import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

import pandas as pd
import pytz

from engine.broker.public_client import PublicAPIError, PublicClient
from engine.data.polygon_rest import PolygonREST
from engine.notify.diagnostics import DiagnosticsReporter, build_row
from engine.notify.discord import Notifier, build_notifier
from engine.risk.event_calendar import EventCalendar
from engine.runtime.entry_detector import EntryDetector
from engine.runtime.kill_switch import KillSwitch
from engine.runtime.market_context import MarketContext, MarketContextBuilder
from engine.runtime.position_monitor import PositionMonitor
from engine.state.store import StateStore
from engine.strategy.iv_engine import IVEngine
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)
ET = pytz.timezone("America/New_York")

PRE_OPEN_TIME = time(9, 0)
MARKET_OPEN = time(9, 30)
ENTRY_START = time(10, 0)
ENTRY_END = time(11, 30)
WIND_DOWN = time(15, 25)
MARKET_CLOSE = time(16, 0)
TICK_SECONDS = 30


@dataclass
class EngineConfig:
    dry_run: bool = True
    tick_seconds: int = TICK_SECONDS
    once: bool = False  # run a single tick then return — for tests / cron


class Engine:
    def __init__(
        self,
        public: Optional[PublicClient] = None,
        polygon: Optional[PolygonREST] = None,
        state: Optional[StateStore] = None,
        notifier: Optional[Notifier] = None,
        engine_config: Optional[EngineConfig] = None,
    ) -> None:
        self.cfg = engine_config or EngineConfig(dry_run=(CONFIG.mode == "DRY_RUN"))
        self.notify = notifier or build_notifier()
        self.state = state or StateStore()
        self.public = public or PublicClient(dry_run=self.cfg.dry_run)
        self.polygon = polygon or PolygonREST()
        self.iv = IVEngine()
        self.events = EventCalendar()
        self.context_builder = MarketContextBuilder(self.polygon, self.iv, self.events)
        self.entry = EntryDetector(self.public, self.state, self.notify)
        self.monitor = PositionMonitor(self.public, self.state, self.notify)
        self.kill = KillSwitch(self.state, self.notify)
        self.diagnostics = DiagnosticsReporter()

        # Per-day diagnostics tallies (reset by morning routine).
        self._diag_eval_count = 0
        self._diag_triggered_count = 0
        self._diag_placed_count = 0
        self._diag_gate_passes = {"regime": 0, "macro": 0, "iv": 0}

        # Daily cache
        self._market_ctx: Optional[MarketContext] = None
        self._market_ctx_date: Optional[date] = None
        self._starting_equity: Optional[float] = None
        self._account_equity: Optional[float] = None

    # ─────────────── high-level ────────────────────────────────────────

    def run(self) -> None:
        """Main loop. Runs forever until SIGINT or `once=True`."""
        self.notify.info(
            f"Harvest starting up — mode={CONFIG.mode}",
            f"DRY_RUN={self.cfg.dry_run}\nTick interval: {self.cfg.tick_seconds}s",
        )
        try:
            while True:
                now_et = datetime.now(ET)
                try:
                    self.tick(now_et)
                except Exception as exc:  # noqa: BLE001 — never let one tick crash the engine
                    log.exception("Unhandled error in tick: %s", exc)
                    self.kill.record_api_error(now_et)
                    self.notify.error("Engine tick error", str(exc)[:1500])

                if self.cfg.once:
                    return
                time_mod.sleep(self.cfg.tick_seconds)
        except KeyboardInterrupt:
            log.warning("KeyboardInterrupt — shutting down")
            self.notify.warn("Harvest shutting down", "Received SIGINT")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        try:
            self.public.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.polygon.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.iv.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.state.close()
        except Exception:  # noqa: BLE001
            pass

    # ─────────────── one tick ──────────────────────────────────────────

    def tick(self, now_et: datetime) -> None:
        """One iteration of the loop."""
        # Skip non-trading days
        if not self._is_trading_day(now_et.date()):
            log.debug("Non-trading day %s — sleeping", now_et.date())
            return

        t = now_et.time()

        # 1. Pre-open setup (09:00–09:30): build context & snapshot equity
        if t >= PRE_OPEN_TIME and self._market_ctx_date != now_et.date():
            self._morning_routine(now_et)

        # 2. Always reconcile pending → filled and check exits during market hours
        if MARKET_OPEN <= t < MARKET_CLOSE:
            self.monitor.reconcile_pending(now_et)
            self.monitor.check_exits(now_et)
            self.monitor.reconcile_closes(now_et)

        # 3. Kill switch evaluation (continuous)
        vix_now = self._market_ctx.vix_value if self._market_ctx else None
        if self.kill.evaluate(
            starting_equity=self._starting_equity,
            current_vix=vix_now,
            now_et=now_et,
        ):
            return  # halted — skip new entries

        # 4. Entry window (10:00–11:30)
        if ENTRY_START <= t <= ENTRY_END and self._market_ctx is not None:
            self._entry_routine(now_et)

        # 5. End-of-day summary at 16:00
        if t >= MARKET_CLOSE and self.state.get_meta("last_eod_summary_date") != now_et.date().isoformat():
            self._eod_summary(now_et)

    # ─────────────── routines ──────────────────────────────────────────

    def _morning_routine(self, now_et: datetime) -> None:
        log.info("Morning routine for %s", now_et.date().isoformat())
        # Reset per-day diagnostics counters.
        self._diag_eval_count = 0
        self._diag_triggered_count = 0
        self._diag_placed_count = 0
        self._diag_gate_passes = {"regime": 0, "macro": 0, "iv": 0}
        try:
            # Snapshot equity for the day's drawdown calc
            equity = self._snapshot_equity()
            self._starting_equity = equity
            self._account_equity = equity
            self.state.set_meta("starting_equity_" + now_et.date().isoformat(), str(equity))

            # Fetch today's ATM IV per underlying for the IV gate
            today_iv = self._fetch_today_atm_iv(now_et.date())

            # Build the daily market context
            self._market_ctx = self.context_builder.build(today_iv, now_et=now_et)
            self._market_ctx_date = now_et.date()

            self.notify.info(
                f"Morning context — {now_et.date().isoformat()}",
                self._summarize_context(self._market_ctx, equity),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Morning routine failed: %s", exc)
            self.notify.error("Morning routine failed", str(exc)[:1500])

    def _entry_routine(self, now_et: datetime) -> None:
        if self._account_equity is None:
            log.warning("No account equity — skipping entry routine")
            return
        diag_rows = []
        for sym in CONFIG.underlyings:
            try:
                bars = self.polygon.intraday_5m(sym, lookback_minutes=180)
                decision = self.entry.check_underlying(
                    symbol=sym,
                    bars_5m=bars,
                    market=self._market_ctx,  # type: ignore[arg-type]
                    account_equity=self._account_equity,
                    now_et=now_et,
                )
                if decision.placed_order_id:
                    log.info("Entry placed for %s — %s", sym, decision.placed_order_id)
                elif decision.triggered and decision.blocked_reason:
                    log.info("Entry triggered but blocked for %s: %s", sym, decision.blocked_reason)

                # Tally diagnostics state.
                self._diag_eval_count += 1
                if decision.triggered:
                    self._diag_triggered_count += 1
                if decision.placed_order_id:
                    self._diag_placed_count += 1
                if self._market_ctx is not None:
                    if not self._market_ctx.blackout_active and self._market_ctx.vix_gate_passed:
                        self._diag_gate_passes["macro"] += 1
                    ctx = self._market_ctx.underlyings.get(sym)
                    if ctx is not None and ctx.regime.regime.value != "MIXED":
                        self._diag_gate_passes["regime"] += 1
                    if ctx is not None and ctx.iv_gate_passed:
                        self._diag_gate_passes["iv"] += 1
                if self._market_ctx is not None:
                    diag_rows.append(build_row(sym, self._market_ctx, decision))
            except Exception as exc:  # noqa: BLE001
                log.exception("Entry check %s failed: %s", sym, exc)
                self.kill.record_api_error(now_et)

        # Emit diagnostics (rate-limited inside the reporter; safe to call every tick).
        if diag_rows:
            try:
                self.diagnostics.maybe_post_snapshot(now_et, diag_rows)
                self.diagnostics.maybe_post_near_misses(now_et, diag_rows)
            except Exception as exc:  # noqa: BLE001 — diagnostics never block trading
                log.warning("Diagnostics post failed: %s", exc)

    def _eod_summary(self, now_et: datetime) -> None:
        try:
            ending_equity = self._snapshot_equity()
            self.state.upsert_daily_pnl(
                now_et.date(),
                starting_equity=self._starting_equity,
                ending_equity=ending_equity,
            )
            closed = self.state.trades_closed_on(now_et.date())
            realized = sum(t.pnl or 0 for t in closed)
            wins = sum(1 for t in closed if (t.pnl or 0) > 0)
            losses = sum(1 for t in closed if (t.pnl or 0) < 0)
            self.notify.info(
                f"EOD summary — {now_et.date().isoformat()}",
                f"Trades closed: {len(closed)}  •  Wins: {wins}  •  Losses: {losses}\n"
                f"Realized P&L: **${realized:+,.2f}**\n"
                f"Equity: ${self._starting_equity or 0:,.2f} → ${ending_equity:,.2f}",
            )
            self.state.set_meta("last_eod_summary_date", now_et.date().isoformat())
            # Diagnostic EOD on the secondary channel.
            try:
                self.diagnostics.post_eod_summary(
                    now_et,
                    self._diag_gate_passes,
                    self._diag_triggered_count,
                    self._diag_placed_count,
                    self._diag_eval_count,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Diagnostics EOD post failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("EOD summary failed: %s", exc)

    # ─────────────── helpers ───────────────────────────────────────────

    def _is_trading_day(self, d: date) -> bool:
        # Use the event calendar's NYSE schedule
        sched = self.events._nyse.schedule(start_date=d, end_date=d)  # type: ignore[attr-defined]
        return not sched.empty

    def _snapshot_equity(self) -> float:
        try:
            p = self.public.get_portfolio()
        except PublicAPIError as exc:
            log.warning("Portfolio fetch failed in snapshot_equity: %s", exc)
            return 0.0
        for e in p.equity:
            if e.type in ("TOTAL_EQUITY", "EQUITY", "TOTAL"):
                try:
                    return float(e.value)
                except (TypeError, ValueError):
                    pass
        # Fallback: buyingPower as a (poor) proxy
        try:
            return float(p.buyingPower.buyingPower or 0)
        except (TypeError, ValueError):
            return 0.0

    def _fetch_today_atm_iv(self, today: date) -> dict[str, float]:
        """Pull the option chain at ~30 DTE for each underlying and grab ATM IV.

        We don't need this exactly at 30 DTE — closest available is fine.
        """
        out: dict[str, float] = {}
        for sym in CONFIG.underlyings:
            try:
                exps = self.public.get_option_expirations(sym).expirations
            except PublicAPIError as exc:
                log.warning("Expirations failed for %s: %s", sym, exc)
                continue
            if not exps:
                continue
            target_dte = 30
            best_exp = min(
                exps,
                key=lambda s: abs((date.fromisoformat(s) - today).days - target_dte),
                default=None,
            )
            if best_exp is None:
                continue
            try:
                chain = self.public.get_option_chain(sym, best_exp)
            except PublicAPIError as exc:
                log.warning("Chain failed for %s %s: %s", sym, best_exp, exc)
                continue
            # ATM = closest call to spot. Use the call set; pick smallest |strike − spot|.
            try:
                spot = float(self.public.get_quote(sym, "EQUITY").last or 0)
            except (PublicAPIError, ValueError, TypeError):
                spot = 0.0
            if spot <= 0 or not chain.calls:
                continue
            best = None
            best_dist = float("inf")
            for c in chain.calls:
                try:
                    k = float(c.strikePrice or 0)
                except (TypeError, ValueError):
                    continue
                d = abs(k - spot)
                if d < best_dist:
                    best_dist = d
                    best = c
            if best is None or best.impliedVolatility is None:
                continue
            try:
                iv_val = float(best.impliedVolatility) * 100.0  # store as %
            except (TypeError, ValueError):
                continue
            out[sym] = iv_val
            log.info("ATM IV %s = %.2f%% (exp=%s, strike≈%.2f)", sym, iv_val, best_exp, spot)
        return out

    @staticmethod
    def _summarize_context(ctx: MarketContext, equity: float) -> str:
        lines = [
            f"Equity: ${equity:,.2f}",
            f"VIX: {ctx.vix_value:.2f}  •  blackout: {'YES — ' + (ctx.blackout_reason or '') if ctx.blackout_active else 'no'}",
        ]
        for sym, u in ctx.underlyings.items():
            lines.append(
                f"{sym}: {u.regime.regime.value}  close={u.regime.close:.2f}  "
                f"sma50={u.regime.sma50:.2f}  sma200={u.regime.sma200:.2f}  "
                f"IVR={u.iv.ivr:.0f}  IVP={u.iv.ivp:.0f}  iv_gate={u.iv_gate_passed}"
            )
        return "\n".join(lines)

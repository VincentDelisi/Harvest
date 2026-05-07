"""End-to-end smoke check: pulls daily bars from Polygon, computes regime,
checks event calendar, and prints status for SPY/QQQ/IWM.

Requires POLYGON_API_KEY in .env. Useful as the first thing to run after
setup to verify everything wires together.

Usage:
    python -m scripts.check_today
"""
from __future__ import annotations

import sys
from datetime import datetime

import pytz
from rich.console import Console
from rich.table import Table

from engine.data.polygon_rest import PolygonREST
from engine.risk.event_calendar import EventCalendar
from engine.strategy.indicators import latest_value, rsi_wilder
from engine.strategy.iv_engine import IVEngine
from engine.strategy.regime import classify
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)
ET = pytz.timezone("America/New_York")


def main() -> int:
    console = Console()
    console.rule("[bold]Credit Spread Engine — Status Check")
    console.print(f"Mode: [cyan]{CONFIG.mode}[/cyan]")
    console.print(f"Time: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # Event blackout
    cal = EventCalendar()
    blackout = cal.check()
    if blackout.blocked:
        console.print(f"[red]BLOCKED[/red]: {blackout.reason}")
    else:
        console.print("[green]No event blackout active[/green]")

    # Polygon client
    poly = PolygonREST()

    # VIX for global gate
    vix = poly.latest_index_value("I:VIX")
    if vix is not None:
        color = "green" if vix < CONFIG.vix_max else "red"
        console.print(f"VIX: [{color}]{vix:.2f}[/{color}] (gate: < {CONFIG.vix_max})")

    iv_eng = IVEngine()

    # Per-underlying status
    table = Table(title="Per-underlying status")
    for col in ["Sym", "Close", "SMA50", "SMA200", "Regime", "RSI(2) 5m", "Bootstrap IVR/IVP"]:
        table.add_column(col)

    for sym in CONFIG.underlyings:
        try:
            daily = poly.daily_bars(sym, lookback_days=300)
            snap = classify(sym, daily)

            intraday = poly.intraday_5m(sym)
            rsi_val = latest_value(rsi_wilder(intraday["close"], period=2)) if not intraday.empty else None

            # Bootstrap IVR/IVP from VIX proxy
            proxy = CONFIG.vix_proxies[sym]
            proxy_bars = poly.daily_bars(f"I:{proxy}", lookback_days=400)
            stats_str = "n/a"
            if vix is not None and not proxy_bars.empty:
                # Use latest proxy value as "today's IV"
                today_proxy = float(proxy_bars["close"].iloc[-1])
                stats = iv_eng.compute_stats(
                    sym,
                    today_iv=today_proxy,
                    bootstrap_history=proxy_bars["close"].tail(252),
                )
                stats_str = f"IVR={stats.ivr:.0f} IVP={stats.ivp:.0f} ({proxy})"

            table.add_row(
                sym,
                f"{snap.close:.2f}",
                f"{snap.sma50:.2f}" if snap.sma50 == snap.sma50 else "—",
                f"{snap.sma200:.2f}" if snap.sma200 == snap.sma200 else "—",
                snap.regime.value,
                f"{rsi_val:.1f}" if rsi_val is not None else "—",
                stats_str,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Failed for %s", sym)
            table.add_row(sym, "ERR", "—", "—", "—", "—", str(e))

    console.print(table)
    poly.close()
    iv_eng.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

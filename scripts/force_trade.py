"""One-shot force-trade — end-to-end pipeline test.

Skips ALL upstream gates (regime, IV, RSI, event blackout). Goes straight from
chosen symbol → expiration pick → spread builder (relaxed credit floor) →
preflight → broker submit → journal write → Discord alert.

Purpose: validate the full execution pipeline (Public.com auth, multi-leg
order construction, fill notifications, journal, alerts) with one real
1-contract trade.

The script honors `ENGINE_MODE`:
  - DRY_RUN:    logs the would-be order, no real submission, returns a fake ID
  - LIVE_SMALL: submits a real order at 1 contract
  - LIVE:       refused (force_trade is for validation, not full-size deploys)

USAGE
─────
On the server:
    cd /opt/harvest
    sudo -u harvest .venv/bin/python -m scripts.force_trade --symbol QQQ --direction PUT

Optional flags:
    --width 1.0          (default 1.0 — script auto-tries 1→2→3→5 if needed)
    --quantity 1         (default 1 — keep this at 1 for the validation trade)
    --max-credit-floor 0 (default 0.0 — accept any positive credit, bypass 33%)
    --dry-run-anyway     force dry-run regardless of ENGINE_MODE
    --yes                skip the interactive confirm prompt
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime
from typing import Optional

import pytz

from engine.broker.public_client import PublicAPIError, PublicClient
from engine.broker.spread_builder import (
    SpreadCandidate,
    _build_for_width,
)
from engine.notify.discord import build_notifier
from engine.state.store import StateStore, TradeRecord
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)
ET = pytz.timezone("America/New_York")

WIDTHS_TO_TRY = (1.0, 2.0, 3.0, 5.0)


def _pick_expiration_no_gate(
    public: PublicClient, symbol: str, today: date
) -> Optional[str]:
    """Find an expiration in the DTE band [dte_min, dte_max]."""
    exp_resp = public.get_option_expirations(symbol)
    candidates = []
    for exp_str in exp_resp.expirations:
        try:
            exp_date = date.fromisoformat(exp_str)
        except Exception:  # noqa: BLE001
            continue
        dte = (exp_date - today).days
        if CONFIG.dte_min <= dte <= CONFIG.dte_max:
            candidates.append((dte, exp_str))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _build_relaxed(
    chain, symbol: str, direction: str, expiration: str,
    starting_width: float, min_credit_floor: float,
) -> Optional[SpreadCandidate]:
    """Try widths starting from `starting_width`, accepting any positive credit
    (no 33% floor). Returns the first liquid candidate found.

    We temporarily monkey-patch CONFIG.min_credit_to_width so _build_for_width
    accepts thinner credits than the strategy normally would. Restored on exit.
    """
    orig_floor = CONFIG.min_credit_to_width
    CONFIG.min_credit_to_width = min_credit_floor
    try:
        try_order = [w for w in WIDTHS_TO_TRY if w >= starting_width]
        for w in try_order:
            cand, counts = _build_for_width(chain, symbol, direction, expiration, w)
            log.info(
                "force_trade: width=$%.2f — examined=%d short_illiquid=%d "
                "no_long=%d long_illiquid=%d credit_low=%d accepted=%d",
                w, counts.examined, counts.short_illiquid, counts.no_long_strike,
                counts.long_illiquid, counts.credit_too_thin, counts.accepted,
            )
            if cand is not None:
                return cand
    finally:
        CONFIG.min_credit_to_width = orig_floor
    return None


def _confirm(prompt: str) -> bool:
    try:
        reply = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Force a single end-to-end trade.")
    parser.add_argument("--symbol", required=True, choices=["SPY", "QQQ", "IWM"])
    parser.add_argument("--direction", required=True, choices=["PUT", "CALL"])
    parser.add_argument("--width", type=float, default=1.0,
                        help="Starting spread width (auto-widens if needed)")
    parser.add_argument("--quantity", type=int, default=1,
                        help="Number of contracts (keep at 1 for validation)")
    parser.add_argument("--min-credit-floor", type=float, default=0.0,
                        help="Min credit/width ratio. 0.0 = accept any positive credit.")
    parser.add_argument("--dry-run-anyway", action="store_true",
                        help="Force dry-run regardless of ENGINE_MODE")
    parser.add_argument("--yes", action="store_true",
                        help="Skip interactive confirm prompt")
    args = parser.parse_args()

    # Refuse LIVE mode — force_trade is for validation
    if CONFIG.mode == "LIVE" and not args.dry_run_anyway:
        log.error("ENGINE_MODE=LIVE refused. Use LIVE_SMALL or DRY_RUN for force_trade.")
        return 2

    effective_dry_run = args.dry_run_anyway or (CONFIG.mode == "DRY_RUN")

    now_et = datetime.now(ET)
    today = now_et.date()

    print()
    print(f"  force_trade — {args.symbol} {args.direction} @ {now_et:%Y-%m-%d %H:%M:%S %Z}")
    print(f"  ENGINE_MODE: {CONFIG.mode}    effective_dry_run: {effective_dry_run}")
    print(f"  starting width: ${args.width:.2f}    qty: {args.quantity}    "
          f"min_credit_floor: {args.min_credit_floor:.2f}")
    print()

    # Build the broker client. Honor effective_dry_run so DRY_RUN truly stays
    # paper even if the script is invoked from a LIVE_SMALL-configured shell.
    public = PublicClient(dry_run=effective_dry_run)
    notify = build_notifier()
    state = StateStore()

    # 1. Pick expiration in DTE band
    try:
        exp = _pick_expiration_no_gate(public, args.symbol, today)
    except PublicAPIError as exc:
        log.error("Failed to fetch expirations: %s", exc)
        return 1
    if exp is None:
        log.error("No expirations in DTE band [%d,%d] for %s",
                  CONFIG.dte_min, CONFIG.dte_max, args.symbol)
        return 1
    print(f"  Picked expiration: {exp}  (DTE in [{CONFIG.dte_min},{CONFIG.dte_max}])")

    # 2. Fetch chain
    try:
        chain = public.get_option_chain(args.symbol, exp)
    except PublicAPIError as exc:
        log.error("Chain fetch failed: %s", exc)
        return 1
    print(f"  Chain: puts={len(chain.puts)}  calls={len(chain.calls)}")

    # 3. Build a candidate with relaxed credit floor
    cand = _build_relaxed(
        chain, args.symbol, args.direction, exp,
        starting_width=args.width, min_credit_floor=args.min_credit_floor,
    )
    if cand is None:
        log.error("No spread candidate found at any width (1/2/3/5). "
                  "Even with min_credit_floor=%.2f, all delta-band strikes "
                  "failed liquidity gates.", args.min_credit_floor)
        return 1

    print()
    print(f"  Candidate:")
    print(f"    short: {cand.short_symbol}  strike={cand.short_strike}  "
          f"delta={cand.short_delta:.3f}  bid/ask={cand.short_bid}/{cand.short_ask}  "
          f"OI={cand.short_oi}")
    print(f"    long:  {cand.long_symbol}  strike={cand.long_strike}  "
          f"bid/ask={cand.long_bid}/{cand.long_ask}  OI={cand.long_oi}")
    print(f"    width=${cand.width:.2f}  credit=${cand.net_credit:.2f}  "
          f"credit/width={cand.credit_to_width:.1%}")
    print(f"    max_profit/contract=${cand.net_credit*100:.0f}  "
          f"max_loss/contract=${(cand.width-cand.net_credit)*100:.0f}")
    print(f"    total qty: {args.quantity}  total_credit=${cand.net_credit*100*args.quantity:.0f}  "
          f"total_max_loss=${(cand.width-cand.net_credit)*100*args.quantity:.0f}")
    print()

    # 4. Confirm
    if not args.yes:
        prompt = (
            f"Submit this {args.symbol} {args.direction} spread "
            f"({'DRY-RUN' if effective_dry_run else 'LIVE'} - {args.quantity} contract"
            f"{'s' if args.quantity > 1 else ''})?"
        )
        if not _confirm(prompt):
            print("Aborted by user.")
            return 0

    # 5. Submit via broker (preflight runs inside place_multi_leg_order)
    order_id = str(uuid.uuid4())
    try:
        resp = public.place_multi_leg_order(
            legs=cand.legs,
            limit_price=cand.limit_price_str(),
            quantity=args.quantity,
            order_id=order_id,
            time_in_force="DAY",
        )
    except PublicAPIError as exc:
        log.error("Order submission failed: %s", exc)
        notify.error(
            f"force_trade — Order REJECTED — {args.symbol} {args.direction}",
            f"Spread: {cand.short_symbol} / {cand.long_symbol}\nError: {exc}",
        )
        return 1

    print()
    print(f"  Order submitted. order_id={resp.orderId}")

    # 6. Persist trade
    trade = TradeRecord(
        trade_id=order_id,
        underlying=args.symbol,
        direction=args.direction,
        short_strike=cand.short_strike,
        long_strike=cand.long_strike,
        width=cand.width,
        expiration=cand.expiration,
        short_symbol=cand.short_symbol,
        long_symbol=cand.long_symbol,
        quantity=args.quantity,
        credit_received=cand.net_credit,
        open_order_id=resp.orderId,
        open_status="PENDING",
        opened_at=now_et.isoformat(),
        extra={
            "source": "force_trade",
            "short_delta": cand.short_delta,
            "credit_to_width": cand.credit_to_width,
            "mode": CONFIG.mode,
            "dry_run": effective_dry_run,
            "min_credit_floor_used": args.min_credit_floor,
        },
    )
    state.insert_trade(trade)
    print(f"  Journaled trade_id={trade.trade_id}")

    # 7. Discord alert
    mode_label = "DRY_RUN" if effective_dry_run else CONFIG.mode
    notify.info(
        f"force_trade — {args.symbol} {args.direction} credit spread",
        f"{cand.short_symbol} (short) / {cand.long_symbol} (long)\n"
        f"Credit: ${cand.net_credit:.2f}  •  Width: ${cand.width:.2f}  •  "
        f"Qty: {args.quantity}  •  Δ_short: {cand.short_delta:.2f}",
        fields=[
            {"name": "Mode", "value": mode_label, "inline": True},
            {"name": "Source", "value": "force_trade (gates bypassed)", "inline": True},
            {"name": "Order ID", "value": resp.orderId[:16], "inline": True},
            {"name": "Credit/Width", "value": f"{cand.credit_to_width:.1%}", "inline": True},
            {"name": "Max profit", "value": f"${cand.net_credit*100*args.quantity:.0f}", "inline": True},
            {"name": "Max loss", "value": f"${(cand.width-cand.net_credit)*100*args.quantity:.0f}", "inline": True},
        ],
    )
    print(f"  Discord alert sent.")
    print()
    print("  force_trade complete. Engine will monitor this position via its "
          "normal exit loop (profit target, stop, time stop).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

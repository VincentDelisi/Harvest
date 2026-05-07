"""Position sizing per STRATEGY_SPEC.md §9.

Kelly-fraction-aware fixed risk: each spread risks (width − credit) × 100.
Trade quantity = floor((account_equity × risk_fraction) / per_spread_max_loss).
LIVE_SMALL caps quantity at 1.
"""
from __future__ import annotations

from math import floor
from typing import Literal

from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)

Mode = Literal["DRY_RUN", "LIVE_SMALL", "LIVE"]


def per_spread_max_loss(width: float, credit: float) -> float:
    """Max loss in $ per spread (one contract). Always positive."""
    return max(0.01, (width - credit) * 100.0)


def size_position(
    *,
    account_equity: float,
    width: float,
    credit: float,
    mode: Mode,
    filled_trade_count: int,
) -> int:
    """Return integer quantity (number of spreads).

    - DRY_RUN  → 1 contract (we want preflight to validate at a meaningful size)
    - LIVE_SMALL or first 30 filled trades → cap at 1
    - LIVE → risk_fraction × equity / per_spread_max_loss
    """
    if account_equity <= 0:
        log.warning("size_position: account_equity=%.2f — returning 0", account_equity)
        return 0

    if mode == "DRY_RUN":
        return 1

    use_initial_risk = (mode == "LIVE_SMALL") or (filled_trade_count < 30)
    rf = CONFIG.risk_fraction_initial if use_initial_risk else CONFIG.risk_fraction_full

    risk_dollars = account_equity * rf
    per_loss = per_spread_max_loss(width, credit)
    qty = floor(risk_dollars / per_loss) if per_loss > 0 else 0
    qty = max(0, qty)

    # LIVE_SMALL: cap at 1 contract
    if mode == "LIVE_SMALL" or use_initial_risk:
        qty = min(qty, 1)

    log.info(
        "size_position: equity=%.2f rf=%.4f per_loss=%.2f mode=%s filled=%d → qty=%d",
        account_equity, rf, per_loss, mode, filled_trade_count, qty,
    )
    return qty


def aggregate_max_loss_pct(
    open_max_losses: list[float], proposed_max_loss: float, account_equity: float
) -> float:
    """Compute the % of equity at risk if we add the proposed position."""
    if account_equity <= 0:
        return 1.0
    return (sum(open_max_losses) + proposed_max_loss) / account_equity

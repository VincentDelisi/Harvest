"""Persistent engine state — trades, positions, kill switch, daily P&L."""
from engine.state.store import StateStore, TradeRecord, KillSwitchState

__all__ = ["StateStore", "TradeRecord", "KillSwitchState"]

"""Manually reset the kill switch.

After the engine halts, you must investigate the root cause, then run:
    python -m scripts.reset_kill_switch

This is intentionally a separate script — not an API or env flag — so resets
require a deliberate human action.
"""
from __future__ import annotations

import sys

from engine.notify.discord import build_notifier
from engine.runtime.kill_switch import KillSwitch
from engine.state.store import StateStore


def main() -> int:
    state = StateStore()
    notifier = build_notifier()
    ks = KillSwitch(state, notifier)
    current = state.get_kill_switch()
    if not current.active:
        print("Kill switch is not active. Nothing to do.")
        return 0
    print(f"Resetting kill switch (was triggered by: {current.reason})")
    ks.reset()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

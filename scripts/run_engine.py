"""Run the Harvest trading engine.

Usage:
    python -m scripts.run_engine                # uses ENGINE_MODE from .env
    python -m scripts.run_engine --once         # one tick then exit (good for cron / debug)
    python -m scripts.run_engine --dry-run      # force DRY_RUN regardless of .env

This is the entry point invoked by systemd in production.
"""
from __future__ import annotations

import argparse
import sys

from engine.runtime.engine import Engine, EngineConfig
from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harvest credit-spread engine")
    parser.add_argument("--once", action="store_true", help="Run a single tick then exit")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run (no orders)")
    parser.add_argument("--tick-seconds", type=int, default=30, help="Seconds between ticks")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or CONFIG.mode == "DRY_RUN"
    log.warning(
        "Starting Harvest — mode=%s dry_run=%s once=%s tick=%ds",
        CONFIG.mode, dry_run, args.once, args.tick_seconds,
    )

    engine = Engine(
        engine_config=EngineConfig(
            dry_run=dry_run,
            tick_seconds=args.tick_seconds,
            once=args.once,
        )
    )
    engine.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Centralized config loading from .env + yaml. See STRATEGY_SPEC.md §13."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

Mode = Literal["DRY_RUN", "LIVE_SMALL", "LIVE"]


class Config(BaseModel):
    # Public.com
    public_secret: str = Field(default_factory=lambda: os.getenv("PUBLIC_COM_SECRET", ""))
    public_account_id: str = Field(default_factory=lambda: os.getenv("PUBLIC_COM_ACCOUNT_ID", ""))
    public_base_url: str = Field(
        default_factory=lambda: os.getenv("PUBLIC_COM_BASE_URL", "https://api.public.com")
    )

    # Polygon
    polygon_api_key: str = Field(default_factory=lambda: os.getenv("POLYGON_API_KEY", ""))
    polygon_rest_base: str = Field(
        default_factory=lambda: os.getenv("POLYGON_REST_BASE", "https://api.polygon.io")
    )
    polygon_ws_url: str = Field(
        default_factory=lambda: os.getenv("POLYGON_WS_URL", "wss://socket.polygon.io/stocks")
    )
    polygon_indices_ws_url: str = Field(
        default_factory=lambda: os.getenv(
            "POLYGON_INDICES_WS_URL", "wss://socket.polygon.io/indices"
        )
    )
    # Set to true only if your Polygon plan includes Indices (Massive bundle).
    # When false (default), VIX/VXN/RVX are fetched from Yahoo Finance for free.
    polygon_has_indices: bool = Field(
        default_factory=lambda: os.getenv("POLYGON_HAS_INDICES", "false").lower() == "true"
    )

    # Engine
    mode: Mode = Field(default_factory=lambda: os.getenv("ENGINE_MODE", "DRY_RUN"))  # type: ignore
    timezone: str = Field(default_factory=lambda: os.getenv("ENGINE_TIMEZONE", "America/New_York"))
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    sqlite_path: str = Field(default_factory=lambda: os.getenv("SQLITE_PATH", "./data/engine.db"))

    # Alerts
    discord_webhook_url: str = Field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))

    # Strategy constants (from STRATEGY_SPEC.md — do not change here without updating spec)
    underlyings: tuple[str, ...] = ("SPY", "QQQ", "IWM")
    vix_proxies: dict[str, str] = {"SPY": "VIX", "QQQ": "VXN", "IWM": "RVX"}
    delta_min: float = 0.16
    delta_max: float = 0.25
    width: float = 1.0
    min_credit_to_width: float = 0.33
    max_bid_ask_pct: float = 0.10           # short-leg gate (% of mid)
    max_long_leg_abs_spread: float = 0.05   # long-leg gate (absolute $)
    min_open_interest: int = 500
    vix_max: float = 30.0
    ivr_min: float = 20.0
    ivp_min: float = 30.0
    rsi_oversold: float = 10.0
    rsi_overbought: float = 90.0
    profit_target_pct: float = 0.50
    stop_loss_multiple: float = 2.0  # close at loss = 2 * credit
    risk_fraction_initial: float = 0.005
    risk_fraction_full: float = 0.01
    max_positions_per_underlying: int = 2
    max_total_positions: int = 4
    max_aggregate_max_loss_pct: float = 0.05
    kill_switch_daily_loss_pct: float = 0.03
    kill_switch_consecutive_losses: int = 3
    entry_window_start: str = "10:00"
    entry_window_end: str = "11:30"
    dte_min: int = 2
    dte_max: int = 3


CONFIG = Config()

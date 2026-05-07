# Credit Spread Engine

Automated daily/short-DTE credit spread trading on SPY, QQQ, IWM via the Public.com API.

**Status:** v1 — under construction. See [`docs/STRATEGY_SPEC.md`](docs/STRATEGY_SPEC.md) for the source-of-truth strategy specification. Code implements the spec; spec is the contract.

## Architecture

```
┌──────────────────┐     ┌─────────────────┐     ┌────────────────┐
│  Polygon         │     │                 │     │  Public.com    │
│  WebSocket +     │────▶│  Engine (24/7)  │────▶│  REST API      │
│  REST            │     │  on VPS         │     │  (orders)      │
└──────────────────┘     └─────────────────┘     └────────────────┘
                                  │
                                  ▼
                          ┌──────────────┐
                          │  SQLite +    │
                          │  Discord     │
                          │  alerts      │
                          └──────────────┘
```

## Modules

```
engine/
├── data/           # Polygon REST + WebSocket clients
├── strategy/       # Indicators, regime, IV rank, scoring, entry/exit rules
├── broker/         # Public.com API client (orders, positions, chain)
├── risk/           # Position sizing, kill switch, blackout calendar
└── utils/          # Logging, config, persistence
```

## Setup

1. Python 3.11+
2. `cp .env.example .env` and fill in credentials (never commit `.env`)
3. `pip install -r requirements.txt`
4. `python -m engine.main --mode DRY_RUN`

## Modes

- `DRY_RUN`: All logic, no order writes. **Use for ≥4 weeks before live.**
- `LIVE_SMALL`: Real orders, 1-contract cap. First 30 live trades.
- `LIVE`: Full sizing per spec §5.

## Critical reminders

- **The MCP/LLM is never in the order-execution path.** This engine is pure rule-based Python. An optional read-only MCP server lives in a separate repo for monitoring.
- **Source of truth is `docs/STRATEGY_SPEC.md`.** Code disputes are resolved by editing the spec, never by editing code in isolation.
- **Kill switch recovery is manual only.** Never automatic re-arm.

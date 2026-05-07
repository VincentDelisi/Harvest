# Harvest — Credit Spread Engine

Automated daily/short-DTE credit spread trading on SPY, QQQ, IWM via the Public.com API.

**Status:** v1 — strategy core, broker client, and trading engine complete. 88 tests passing.

- Strategy contract: [`docs/STRATEGY_SPEC.md`](docs/STRATEGY_SPEC.md)
- Deployment runbook: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)

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
├── strategy/       # Indicators, regime, IV rank/percentile
├── broker/         # Public.com API client + spread builder
├── risk/           # Event-blackout calendar
├── state/          # SQLite trade ledger, kill-switch flag
├── notify/         # Discord webhook notifier
├── runtime/        # Entry detector, position monitor, kill switch, main loop
└── utils/          # Logging, config
```

## Quick start (local)

1. Python 3.11+
2. `cp .env.example .env` and fill in credentials (never commit `.env`)
3. `pip install -r requirements.txt`
4. `python -m pytest tests/ -v` (all 88 must pass)
5. `python -m scripts.check_today` (sanity-check Polygon + market state)
6. `python -m scripts.run_engine --once --dry-run` (one tick of the engine)
7. `python -m scripts.run_engine --dry-run` (full loop, dry-run)

## Production deploy

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — runs on a $5/mo Hetzner VPS under `systemd`.

```bash
curl -fsSL https://raw.githubusercontent.com/VincentDelisi/Harvest/main/deploy/install.sh | sudo bash
```

## Modes

- `DRY_RUN`: All logic, no order writes. **Use for ≥4 weeks before live.**
- `LIVE_SMALL`: Real orders, 1-contract cap. First 30 live trades.
- `LIVE`: Full sizing per spec §5.

## Critical reminders

- **The MCP/LLM is never in the order-execution path.** This engine is pure rule-based Python. An optional read-only MCP server lives in a separate repo for monitoring.
- **Source of truth is `docs/STRATEGY_SPEC.md`.** Code disputes are resolved by editing the spec, never by editing code in isolation.
- **Kill switch recovery is manual only.** Never automatic re-arm.

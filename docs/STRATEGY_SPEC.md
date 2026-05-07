# Credit Spread Engine — v1 Strategy Specification

**Author:** Vincent Delisi
**Last revised:** 2026-05-06
**Status:** v1 — single strategy, daily index-ETF credit spreads
**Broker:** Public.com (REST API)
**Market data:** Polygon (Massive) for equity bars/quotes; Public for options chain & Greeks

---

## 1. Purpose

A fully-automated system that sells daily/short-DTE credit spreads on SPY, QQQ, and IWM, sized by account risk, with deterministic entries and exits. The system is rule-based — no LLM is in the execution path. The system implements this spec exactly; if reality and the spec disagree, the system follows the spec.

This document is the **source of truth**. Code implements the spec. Disputes are resolved by editing the spec, never by editing the code in isolation.

---

## 2. Universe and instruments

- Underlyings: **SPY, QQQ, IWM** only.
- Strategy: **Vertical credit spreads** — short the inner strike, long the outer strike, same expiration, $1 wide.
  - Bull regime → put credit spreads (bull put spreads)
  - Bear regime → call credit spreads (bear call spreads)
- DTE: **2–3 calendar days** for the first 30 live trades. After 30 trades meet expected statistics, system may drop to **1 DTE**.
- Width: **$1** strikes. Width is fixed in v1; do not vary by underlying or by score.

---

## 3. Regime detection

Regime is determined **once per trading day** at 09:30 ET from the **prior close's daily bar**. It does not change intraday.

Inputs (per underlying, daily timeframe):
- `close` = prior session's close
- `sma50` = 50-period simple moving average of closes
- `sma200` = 200-period simple moving average of closes

Regime classification:
- **BULL** if `close > sma50` AND `sma50 > sma200`
- **BEAR** if `close < sma50` AND `sma50 < sma200`
- **MIXED** otherwise → no trades that day for that underlying

Regime is computed independently per underlying. SPY may be BULL while IWM is MIXED; the system trades each underlying based on its own regime.

---

## 4. Hard gates (all must pass; no exceptions, no overrides)

A candidate trade may only be considered if **every** gate passes. If any gate fails, the candidate is dropped silently and logged.

### 4.1 Underlying-level gates
- Underlying ∈ {SPY, QQQ, IWM}
- Regime ∈ {BULL, BEAR} (not MIXED)
- VIX < 30 (CBOE VIX spot, latest available)
- Today is not an event blackout day (see §7)
- Current time is within entry window: **10:00 ET ≤ now ≤ 11:30 ET**
- No existing position in this underlying that would breach concurrency limits (§9)

### 4.2 Volatility gates
- IV Rank (IVR) ≥ 20 **OR** IV Percentile (IVP) ≥ 30
- IVR/IVP computed against the underlying's own ATM IV history (bootstrap from VIX/VXN/RVX until 252 days of own data exist; see §6)

### 4.3 Strike & spread gates
- Short strike delta is in [0.16, 0.25] (absolute value)
- Spread is $1 wide
- Net credit ≥ 33% of width (i.e., ≥ $0.33 for a $1 spread)
- Bid-ask liquidity:
  - **Short leg** (where execution quality matters most): bid-ask spread ≤ 10% of mid
  - **Long leg** (far OTM, tail cap — small absolute mids make % gates impractical): absolute bid-ask spread ≤ $0.05
  - Both legs must have bid > 0 and ask > 0
- Open interest ≥ 500 on each strike
- Both strikes have non-zero volume today (skip dead strikes)

### 4.4 Trigger gate (entry signal)
- Bull regime: RSI(2) on 5-minute bars of underlying < 10 within the last 15 minutes (short-term oversold pullback inside an uptrend)
- Bear regime: RSI(2) on 5-minute bars > 90 within the last 15 minutes (short-term overbought rip inside a downtrend)

If multiple candidates across underlyings pass all gates simultaneously, prioritize by: (1) highest IVR, (2) highest credit/width ratio, (3) tightest bid-ask. Take at most one new entry per polling cycle.

---

## 5. Position sizing

For each candidate that passes all gates:

```
max_loss_per_contract = (width - credit) * 100
account_risk_dollars = account_equity * risk_fraction
contracts = floor(account_risk_dollars / max_loss_per_contract)
```

- `risk_fraction = 0.005` (0.5%) for the first 30 closed trades
- `risk_fraction = 0.01` (1.0%) thereafter, conditional on realized stats matching expectations

If `contracts < 1`, skip the trade. Never round up.

Account equity is read from Public's portfolio endpoint at the moment of sizing — never cached for more than 60 seconds.

---

## 6. IVR / IVP computation

### 6.1 Source of "today's IV"
- Pull the option chain for the underlying at ~30 DTE (closest available expiration ≥ 25 DTE).
- ATM IV = average of the implied volatilities of the ATM call and ATM put at that expiration (volume-weighted if both have volume; equal-weighted otherwise).
- Compute and persist daily at 15:55 ET to a local SQLite table `iv_history(symbol, date, atm_iv)`.

### 6.2 IVR formula
```
ivr = (today_iv - min(iv, last 252 trading days)) / (max(iv, last 252 trading days) - min(...)) * 100
```

### 6.3 IVP formula
```
ivp = (count of days in last 252 where iv < today_iv) / 252 * 100
```

### 6.4 Bootstrap
Until the SQLite table has ≥ 252 entries for a given symbol, substitute the corresponding CBOE volatility index:
- SPY → VIX
- QQQ → VXN
- IWM → RVX

VIX/VXN/RVX history is available from Polygon (or CBOE direct). Compute IVR/IVP from that index's spot value.

---

## 7. Event blackouts

No new entries on any of the following days, **and** no new entries after 14:00 ET on the prior session.

- Scheduled FOMC meeting days (from [Fed FOMC calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm))
- CPI release days (from [BLS schedule](https://www.bls.gov/schedule/news_release/cpi.htm))
- NFP days (first Friday of each month, except when BLS reschedules)
- Powell scheduled testimony / major speeches (manually flagged in `event_calendar.yaml`)
- Days adjacent to market holidays (early-close days)

Existing positions on event days:
- If position is at ≥ 50% of max profit at 13:00 ET on the prior session, close immediately at the GTC limit.
- Otherwise, hold through; rely on stop loss and tested-strike rules.
- No mechanical close before event purely to "avoid the event."

The blackout calendar is loaded from a YAML file at engine startup and refreshed daily at 06:00 ET.

---

## 8. Exits

Every open position has **four** simultaneous exit conditions monitored continuously. First trigger wins.

### 8.1 Profit target (passive)
- GTC limit order to close at **50% of max profit** placed within 30 seconds of entry fill.
- Order is a multi-leg limit at the price equal to entry credit × 0.50.

### 8.2 Stop loss (active poll)
- Close immediately if current spread mid-price ≥ **3 × entry credit** (i.e., loss = 2 × credit received).
- Closing order is a marketable limit (mid + 1–2 cents) — not a market order, to avoid wide-spread fills outside RTH or in fast markets.

### 8.3 Tested-strike rule (active poll)
- Close immediately if underlying last trade price touches or breaches the short strike, regardless of P&L on the spread.
- Same closing-order logic as 8.2.

### 8.4 Time stop
- At **T-1 DTE 15:25 ET**, close any still-open position at mid-market regardless of P&L.
- The 15:25 cutoff (5 minutes before Public's auto-cancel of pending orders for same-day expiring stock/ETF options at 15:30) is a hard deadline; if a closing order is unfilled by 15:28 ET, escalate to a marketable limit at the bid (we are short the spread; bid is the price to pay to buy it back).
- Goal: never hold to expiration. Never carry assignment / pin risk.
- Source for cutoff: [Public Help — Option expiration & assignment](https://help.public.com/en/articles/8460531-understanding-option-expiration-exercise-and-assignment).

### 8.5 Polling cadence
- Exit monitor evaluates every **15 seconds** during 09:30–16:00 ET.
- WebSocket-driven: any underlying tick that crosses a tested-strike level triggers immediate evaluation regardless of cadence.

---

## 9. Concurrency and portfolio limits

- Max **2 concurrent positions per underlying**.
- Max **4 concurrent positions total** across all underlyings.
- Max **5% of account equity** in aggregate max-loss across all open positions.
- Same-day re-entry into a closed position's underlying is allowed only if the closed position hit profit target, not stop or tested-strike.

---

## 10. Kill switch

The engine halts all new entries (existing positions still managed) when **any** of:

- Realized session P&L ≤ **−3% of account equity**
- 3 consecutive losing trades in a single session
- VIX prints ≥ 30 intraday
- Public API returns ≥ 5 errors in a 60-second window
- Polygon WebSocket disconnects for > 2 minutes
- Manual halt flag set in `state.yaml`

Recovery is **manual only**. No automatic re-arm. Human reviews logs, clears the flag, restarts the engine.

---

## 11. Order placement procedure

Every order follows this sequence with no exceptions:

1. Build the multi-leg order payload.
2. Call `POST /preflight/multi-leg`. If the response indicates rejection, abort and log.
3. If preflight passes, generate a fresh UUID v4 for `requestId`.
4. Call `POST /trading/{accountId}/order`.
5. Poll `GET /trading/{accountId}/order/{orderId}` every 1 second for up to 30 seconds.
6. If filled, log fill and proceed to place the GTC profit-target order (§8.1).
7. If not filled in 30 seconds, cancel via `DELETE` and log as "no fill — moved on." Do not chase.

All orders are **limit orders**. The system does not place market orders for entries, ever. Entry limit price = mid of the spread, refreshed at preflight time.

---

## 12. Logging and observability

Every event is logged to:
- `engine.log` (rotating file, 30-day retention)
- SQLite `trades.db` (one row per trade lifecycle: entry, fills, exits, P&L)
- Discord webhook for: entry, exit, kill-switch, errors

Daily 16:30 ET summary email/Discord post: trades count, win rate, avg win, avg loss, P&L, current open positions, regime per underlying.

---

## 13. Modes

The engine has three operating modes, set in `config.yaml`:

- **DRY_RUN**: All logic runs; no API writes occur. Orders are logged as "would have placed." Use for at least 4 weeks before live.
- **LIVE_SMALL**: Real orders, hard cap at 1 contract per trade regardless of sizing math. Use for first 30 live trades.
- **LIVE**: Full sizing per §5.

Mode transitions require manual config edit + restart. Never automatic.

---

## 14. What is explicitly out of scope for v1

The following are deliberately NOT in v1. They are tracked in `BACKLOG.md` and can be added one at a time after v1 has 60+ live trades:

- Iron condors (both sides simultaneously)
- Single-name underlyings (AAPL, NVDA, etc.)
- Earnings plays
- Wider widths ($2, $5)
- Dynamic delta selection by IVR
- Machine-learned scoring (current scoring is rule-based weighted composite)
- LLM/MCP write access to orders
- Dynamic profit-take by DTE (e.g., 25% on 1DTE, 50% on 3DTE)
- Rolling losing positions

Adding any of these without explicit revision to this spec is a process violation.

---

## 15. Open questions / decisions deferred

- Exact RSI(2) threshold: 10/90 is starting point, may tune to 5/95 or 15/85 after dry-run
- Whether to use VIX9D vs. VIX for the 1DTE IV regime check (defer to phase 2)
- Whether to add a "no trade if last 2 SPY daily candles are doji" filter (defer; track in BACKLOG)

---

## 16. Revision log

- 2026-05-06: v1 initial spec (this document)
- 2026-05-06: §8.4 time stop tightened from 15:30 to 15:25 ET to stay ahead of Public's auto-cancel of same-day-expiring stock/ETF option orders at 15:30 ET.
- 2026-05-06: §4.3 bid-ask gate split: 10%-of-mid stays for the short leg; long leg uses absolute ≤ $0.05 because far-OTM contracts have tiny mids that make percentage gates impractical.

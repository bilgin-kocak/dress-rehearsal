# Dress Rehearsal — Build Spec

**One line:** A drop-in paper twin of the Binance Agent OS MCP server, plus a rehearsal harness that runs your agent against the twin, scores it, gates go-live, and then shadows it once it trades for real.

**Tagline for the pitch:** *Same agent. Same prompt. Change one URL.*

**Target:** Binance Agent OS Mini Hackathon — Track A (agent + video + repo) and Track B (live trades at the end). Deadline 2026-09-08 23:59 UTC. Build window ≈ 2.5 days. Everything in this spec is scoped to that window; stretch items are marked **[STRETCH]** and must not block the MVP.

---

## 0. Why this exists (context for the builder agent)

Binance Agent OS gives an agent a live, funded "Agentic" sub-account behind a hosted MCP endpoint (`https://agent.binance.com/mcp/agentic`, OAuth, scopes: market data / account / trade / transfer, no withdrawal). There is **no paper or testnet mode** for this endpoint. Kraken's MCP defaults to paper mode and marks live trading as "dangerous"; Coinbase for Agents offers a sandbox. Binance offers a live empty box you fund. The only loss cap is the balance you transfer in.

Dress Rehearsal closes that gap without touching Binance's side: a local MCP server that exposes **exactly the same tools** as the real agentic endpoint, backed by live public Binance market data and a simulated ledger. Any MCP client (Claude Code, Codex, Cursor, ChatGPT via connector) points at the twin during development, gets a scored go-live report, then flips to the real URL.

Design principles, in priority order:

1. **Schema fidelity over engine fidelity.** The agent must not be able to tell the twin from the real server by tool names, parameter shapes, or error formats. Fill realism matters, but a mismatched schema kills the product.
2. **Live data, fake money.** No synthetic prices. Fills are computed against the real public order book / trade stream at the moment of the call.
3. **Zero code changes in the agent.** Switching paper → live is a config change only.
4. **Deterministic when asked.** Replay mode uses recorded market data so tests and reports are reproducible.
5. **Ship the demo path first.** Anything not visible in the 2:20 video is second priority.

---

## 1. Deliverables

| # | Deliverable | Track |
|---|---|---|
| D1 | `rehearsal serve` — local MCP twin server (stdio + streamable HTTP) mirroring the Binance agentic tool schema | A |
| D2 | Fill engine + virtual ledger (spot MVP; USDⓈ-M futures basic; transfer) | A |
| D3 | `rehearsal run` — headless rehearsal runner driving the agent N sessions against the twin | A |
| D4 | Go-live report (Markdown + JSON) and `rehearsal gate` (pass/fail + the exact flip command) | A |
| D5 | Shadow mode — mirrors real tool calls into the twin, produces divergence report | A + B |
| D6 | `skills/dress-rehearsal/SKILL.md` — the Rehearsal Agent as an installable skill (Skill Hub format) | A |
| D7 | Minimal dashboard (single HTML page) for the video | A |
| D8 | README, ARCHITECTURE.md, 2:20 video, X post with repo link, survey, ≥3 real live trades logged through the flow | A + B |

---

## 2. Architecture

```
                       ┌──────────────────────────────────────────────┐
                       │  Agent client (Claude Code / Codex / Cursor) │
                       └──────────────┬───────────────────────────────┘
                                      │ MCP (stdio or streamable HTTP)
                      paper           │            live
             ┌────────────────────────┴───────────────────────────┐
             ▼                                                    ▼
   ┌──────────────────────┐                        ┌──────────────────────────┐
   │  TWIN MCP SERVER     │                        │  Binance Agentic MCP     │
   │  (this repo)         │◄── shadow events ──────│  agent.binance.com       │
   │  tools mirrored from │   (Claude Code hook)   └──────────────────────────┘
   │  schemas/tools.json  │
   └──────────┬───────────┘
              │
   ┌──────────┴───────────┐     ┌───────────────────┐     ┌─────────────────────┐
   │  Fill Engine         │◄────│  Market Data Feed │◄────│ Binance public REST │
   │  (spot / usdm)       │     │  (REST + WS)      │     │ + WebSocket streams │
   └──────────┬───────────┘     └───────────────────┘     └─────────────────────┘
              │
   ┌──────────┴───────────┐     ┌───────────────────┐     ┌─────────────────────┐
   │  Ledger (SQLite)     │────►│  Report / Gate    │────►│ Dashboard (HTML)    │
   └──────────────────────┘     └───────────────────┘     └─────────────────────┘
```

Single Python package `rehearsal/`. One process runs the MCP server, the market-data feed, the fill engine, and the HTTP dashboard/API. SQLite for state. No external services.

---

## 3. Repo layout

```
dress-rehearsal/
├── README.md
├── ARCHITECTURE.md
├── LICENSE                      (MIT)
├── pyproject.toml               (python >=3.11)
├── rehearsal.example.yaml
├── schemas/
│   ├── tools.json               ← dumped from the REAL Binance agentic MCP on Day 0 (see §4)
│   ├── tool_map.yaml            ← tool name → twin handler category + arg mapping
│   └── fallback_tools.json      ← hand-written fallback if the dump fails (based on documented scopes)
├── rehearsal/
│   ├── __init__.py
│   ├── cli.py                   (typer)
│   ├── config.py                (pydantic-settings; loads rehearsal.yaml)
│   ├── server/
│   │   ├── mcp_server.py        (FastMCP; builds tools dynamically from schemas/tools.json)
│   │   ├── handlers.py          (category handlers: market_data, account, trade, transfer)
│   │   ├── confirm.py           (confirmation modes: none | elicitation | echo)
│   │   └── errors.py            (Binance-shaped error payloads)
│   ├── market/
│   │   ├── feed.py              (REST snapshots + WS streams; cache; replay source)
│   │   ├── binance_public.py    (spot: api.binance.com; usdm: fapi.binance.com; no auth)
│   │   ├── exchange_info.py     (filters: PRICE_FILTER, LOT_SIZE, NOTIONAL, …; TTL cache)
│   │   └── recorder.py          (record depth/aggTrade/markPrice to .jsonl for replay)
│   ├── engine/
│   │   ├── ledger.py            (wallets, orders, fills, positions, transfers; SQLite via sqlite3/SQLModel)
│   │   ├── fills_spot.py
│   │   ├── fills_usdm.py
│   │   ├── filters.py           (validate + round qty/price like Binance; reject with real error codes)
│   │   ├── fees.py
│   │   └── latency.py           (configurable latency model)
│   ├── rehearsal/
│   │   ├── runner.py            (drives agent sessions; adapters below)
│   │   ├── adapters/
│   │   │   ├── base.py
│   │   │   ├── claude_code.py   (claude -p … --mcp-config …)
│   │   │   └── codex.py         (codex exec …)   [STRETCH if time short; interface only]
│   │   ├── metrics.py
│   │   ├── report.py            (Markdown + JSON)
│   │   └── gate.py
│   ├── shadow/
│   │   ├── receiver.py          (POST /shadow/event)
│   │   ├── hook.py              (generates Claude Code hook config + the hook script)
│   │   └── divergence.py
│   ├── dashboard/
│   │   ├── app.py               (FastAPI; mounts MCP HTTP transport + /api + static)
│   │   └── static/index.html    (vanilla JS; polls /api/state every 1s)
│   └── policy.py                (max notional / symbol allowlist / leverage cap — counts violations, does NOT block by default)
├── skills/
│   └── dress-rehearsal/
│       └── SKILL.md             (Rehearsal Agent instructions; Skill Hub frontmatter)
├── prompts/
│   ├── strategy_funding_carry.md
│   ├── strategy_simple_momentum.md
│   └── strategy_deliberately_bad.md   (for the demo: over-sizes, wrong precision, ignores stop)
├── fixtures/
│   └── replay/                  (recorded market data used by tests + deterministic demo)
├── tests/
│   ├── test_filters.py
│   ├── test_fills_spot.py
│   ├── test_fills_usdm.py
│   ├── test_ledger.py
│   ├── test_mcp_roundtrip.py
│   └── test_gate.py
└── scripts/
    ├── dump_tools.md            (step-by-step: how to get schemas/tools.json from the real server)
    └── demo.sh                  (starts twin in replay mode with the demo fixture)
```

---

## 4. Day-0 blocker: mirror the real schema

**This is the first task. Do not write handlers before this is done.**

The public docs describe capabilities, not tool schemas. The twin must be generated from the real server's `tools/list` response.

Procedure (`scripts/dump_tools.md`):

1. `claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic` → complete OAuth with an eligible account (do NOT fund yet).
2. In Claude Code, ask it to list every tool exposed by `binance-mcp-server` **with the full JSON input schema and description**, and write the raw list to `schemas/tools.json`. Alternative: use the MCP Inspector (`npx @modelcontextprotocol/inspector`) against the endpoint after OAuth, export `tools/list`.
3. Also capture, for **one read-only call per category**, the exact result shape (JSON) into `schemas/samples/<tool>.json`. For trade tools, capture the result of a **rejected** call (e.g. symbol `FOOBAR`) so error shape is known without spending money.
4. Record how confirmation works for write tools: (a) does the server use MCP **elicitation**? (b) does the tool description instruct the model to restate and wait? (c) is there a parameter like `confirm: true`? Write the answer into `schemas/CONFIRMATION.md`. The twin's `confirm.py` implements whichever it is.
5. Note whether write tools accept a client order id parameter (name and max length). If yes, the twin must accept and echo it identically.

`schemas/tool_map.yaml` then maps each real tool name to a twin handler:

```yaml
# example — real names will differ; fill in from tools.json
tools:
  get_ticker:            {category: market_data, handler: ticker}
  get_order_book:        {category: market_data, handler: depth}
  get_klines:            {category: market_data, handler: klines}
  get_funding_rate:      {category: market_data, handler: funding}
  get_account_balances:  {category: account,     handler: balances}
  get_positions:         {category: account,     handler: positions}
  get_open_orders:       {category: account,     handler: open_orders}
  get_order_history:     {category: account,     handler: order_history}
  place_spot_order:      {category: trade,       handler: place_order, market: spot}
  place_futures_order:   {category: trade,       handler: place_order, market: usdm}
  cancel_order:          {category: trade,       handler: cancel_order}
  cancel_all_orders:     {category: trade,       handler: cancel_all}
  transfer:              {category: transfer,    handler: transfer}
unmapped_policy: passthrough_public   # market-data-looking tools → try public API by name heuristics; else error TWIN_UNSUPPORTED
```

`mcp_server.py` reads `tools.json`, registers each tool with the **verbatim** name, description, and inputSchema, and dispatches by `tool_map.yaml`. The twin never invents tool names. If `tools.json` is missing, it loads `fallback_tools.json` and logs a loud warning banner in the dashboard ("SCHEMA NOT MIRRORED — fidelity unknown").

Acceptance: a diff of `tools/list` between real and twin shows **zero** differences in names and inputSchemas (descriptions may carry a `[TWIN]` prefix only if `config.mark_twin_descriptions: true`; default false).

---

## 5. Twin MCP server

### 5.1 Transports
- `stdio` (default for Claude Code / Codex local).
- `streamable-http` on `http://127.0.0.1:8765/mcp` (for clients that only take URLs, and for the demo "change one URL" moment).
- No auth by default; optional static bearer token via config.

### 5.2 Confirmation modes (`confirm.py`)
- `none` — write tools execute immediately (client-side approval, e.g. Claude Code permission prompt, still applies).
- `elicitation` — before executing a write tool, send an MCP elicitation request restating symbol/side/type/qty/price; execute only on accept. Use if the real server does this.
- `echo` — do not elicit; return a structured `{"status":"PENDING_CONFIRMATION", "restatement": "...", "confirm_token": "..."}` and require a second call with the token. Use only if the real server does this.
- Default is set from `schemas/CONFIRMATION.md` findings on Day 0. The runner records **confirmation compliance**: did the agent restate before a write in `none` mode? (Detected from transcript: a restatement line containing symbol+side+qty within the two assistant turns before the call.)

### 5.3 Handlers
All handlers return results shaped like `schemas/samples/*.json`. Numbers are returned as **strings** if the real server returns strings (Binance REST does); match the sample exactly.

- `market_data.*` → straight passthrough to Binance public REST/WS via `feed.py` (cached ≤ 500 ms for ticker/depth, ≤ 1 min for klines/exchangeInfo). In replay mode, served from fixtures.
- `account.balances` → ledger wallets. `account.positions` → ledger positions with live mark price. `account.open_orders` / `order_history` → ledger.
- `trade.place_order` → `filters.validate()` → `fee/latency` → `fills_<market>.submit()` → ledger → response. Rejections use Binance error shape `{"code": -1013, "msg": "Filter failure: LOT_SIZE"}` (exact codes/messages from Binance docs; keep a table in `errors.py`).
- `trade.cancel_order` / `cancel_all` → ledger + engine.
- `transfer` → between `spot` and `usdm` wallets inside the virtual sub-account only. Any asset not present → Binance-style insufficient balance error.
- Any tool that would withdraw or touch a main account: **does not exist** (mirror the real scope). If a mapped tool is unsupported in twin → `{"code": -9001, "msg": "TWIN_UNSUPPORTED: <tool>"}` and a dashboard warning.

### 5.4 Tool-call log
Every call: `{ts, session_id, tool, args, result_status, result, latency_ms, error_code}` → `tool_calls` table. This is the raw material for metrics and the dashboard.

---

## 6. Market data feed (`market/`)

- Spot public REST: `https://api.binance.com` — `/api/v3/exchangeInfo`, `/api/v3/depth?limit=100`, `/api/v3/ticker/bookTicker`, `/api/v3/ticker/24hr`, `/api/v3/klines`, `/api/v3/aggTrades`.
- USDⓈ-M public REST: `https://fapi.binance.com` — `/fapi/v1/exchangeInfo`, `/fapi/v1/depth`, `/fapi/v1/premiumIndex` (mark price + funding), `/fapi/v1/fundingRate`, `/fapi/v1/klines`.
- WebSocket (spot `wss://stream.binance.com:9443/stream`, usdm `wss://fstream.binance.com/stream`): `<symbol>@depth20@100ms`, `<symbol>@aggTrade`, `<symbol>@bookTicker`, usdm `<symbol>@markPrice@1s`. Subscribe lazily per symbol on first use; unsubscribe after `idle_ttl`.
- Respect public rate limits (weight headers); back off on 429/418.
- `recorder.py`: writes each subscribed stream to `fixtures/replay/<symbol>/<date>.jsonl`. `feed.py` with `mode: replay` reads those at a configurable speed (1x, 10x, "as-fast-as-possible") with a virtual clock. All engine code takes `now()` from the feed's clock, never from `time.time()`.

---

## 7. Fill engine

### 7.1 Common
- Validate against exchangeInfo filters before anything else: `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize/minQty/maxQty`, `NOTIONAL/MIN_NOTIONAL`, `MARKET_LOT_SIZE`, `PERCENT_PRICE_BY_SIDE` (spot). Reject with the real Binance code + message. **Do not auto-round** unless the real server does (check Day-0 samples); a rejection is a feature — it is the "hallucinated parameter" catch.
- Latency model: `latency.py` draws `d ~ max(0, N(mean_ms, sd_ms))` (defaults 80 ms / 30 ms) and the order is matched against the book **as of `now()+d`** (next available snapshot). Config allows a fixed value for determinism.
- Fees: spot taker 0.10% / maker 0.10% default (configurable; BNB discount off); usdm taker 0.05% / maker 0.02%. Fee asset = quote asset.
- Order ids: monotonically increasing ints per market + `clientOrderId` echo if the real server exposes it.

### 7.2 Spot
- `MARKET`: walk the depth (≤100 levels) on the opposite side; fill across levels; if depth insufficient → fill what exists, mark remainder `EXPIRED` (Binance behaviour for IOC-like market) — configurable to `reject`.
- `LIMIT` (GTC): if marketable (buy limit ≥ best ask / sell limit ≤ best bid) → fill immediately as taker against the book up to the limit price. Otherwise rest. A resting order fills when the aggTrade stream prints **at or through** its price; to approximate queue position, require cumulative traded volume at/through the price since placement ≥ `order.qty × queue_factor` (default 1.0; shadow calibration adjusts it). Partial fills allowed. Maker fee.
- `LIMIT` IOC / FOK: fill immediately or cancel/reject accordingly.
- `LIMIT_MAKER`: reject if marketable (code -2010 "Order would immediately match and take.").
- `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT`: trigger when last trade price crosses `stopPrice`; then behave as MARKET/LIMIT.
- `OCO` **[STRETCH]** only if the real tool list has it.

### 7.3 USDⓈ-M futures (basic)
- Isolated margin only (cross **[STRETCH]**). Per-symbol leverage settable if the real server exposes it; else default 5x, configurable.
- Mark price from `markPrice` stream drives unrealized PnL and liquidation; last price drives stop triggers.
- Open/close/increase/reduce logic with `reduceOnly` support. Realized PnL on close.
- Liquidation: when `margin_balance ≤ maintenance_margin` (maintenance rate from a small table per symbol, default 0.5% for the largest bracket) → position closed at mark price minus a configurable liquidation fee; event logged as `LIQUIDATION` (this is the demo's "blow-up in the twin" moment).
- Funding: at 00:00 / 08:00 / 16:00 UTC (virtual clock) apply `position_notional × funding_rate` from `/fapi/v1/fundingRate` (or replay fixture).

### 7.4 Ledger (`ledger.py`)
Tables: `wallets(market, asset, free, locked)`, `orders`, `fills`, `positions`, `transfers`, `tool_calls`, `sessions`, `events` (liquidation, funding, policy_violation, confirmation_missing). Every balance change is an event with a reason. `rehearsal reset` recreates the DB from `initial_balances` in config.

---

## 8. Rehearsal runner, metrics, report, gate

### 8.1 Runner (`rehearsal run`)
```
rehearsal run --strategy prompts/strategy_funding_carry.md \
              --sessions 5 --client claude-code \
              --mode replay --fixture fixtures/replay/demo_2026-09-05 \
              --session-minutes 30
```
- For each session: reset ledger to `initial_balances`, start (or reuse) the twin, launch the client adapter headless with the strategy prompt, wait for completion or timeout, collect the transcript (`--output-format json` for Claude Code), close session.
- `adapters/claude_code.py`: `claude -p "<prompt>" --mcp-config <generated.json> --output-format json --max-turns N`, with `<generated.json>` pointing `binance-mcp-server` at the twin (**same server name as the real one** so the strategy prompt never changes).
- `adapters/codex.py`: `codex exec` equivalent — interface only if time is short.
- Runner tags every tool call in the DB with `session_id`.

### 8.2 Metrics (`metrics.py`), computed per session and aggregated
- P&L: net after fees; return on initial equity; max drawdown; Sharpe-like (per-session, informational).
- Execution quality: average slippage in bps vs mid at decision time (mid captured at tool-call arrival); fill rate of limit orders; time-to-fill p50/p95.
- Robustness: count and rate of **rejected calls** by error code (LOT_SIZE, PRICE_FILTER, NOTIONAL, unknown symbol, insufficient balance); count of `TWIN_UNSUPPORTED` calls; tool-call latency p50/p95; loops (same failing call ≥3× in a row).
- Safety: policy violations from `policy.py` (max notional per order, max gross exposure, symbol allowlist, leverage cap, max orders per minute) — counted, not blocked, unless `policy.enforce: true`; liquidation events; confirmation compliance (% of writes preceded by a restatement, when `confirm_mode: none`).
- Behaviour: number of sessions where the agent flattened before exit; did it cancel stale orders.

### 8.3 Report (`report.py`)
`reports/<run_id>/report.md` + `report.json`. Markdown sections: Summary (pass/fail badge), Equity curve (ASCII sparkline + PNG if matplotlib available), Trades table, Rejections by code, Policy violations, Confirmation compliance, Notable events (liquidations, loops), Recommendations (rule-based one-liners: e.g. "12 LOT_SIZE rejections on ETHUSDT — agent is not rounding to stepSize 0.0001").

### 8.4 Gate (`rehearsal gate`)
Thresholds in `rehearsal.yaml`:
```yaml
gate:
  min_sessions: 3
  max_drawdown_pct: 8
  max_rejection_rate: 0.10
  max_policy_violations: 0
  max_liquidations: 0
  min_confirmation_compliance: 0.95
  min_limit_fill_rate: 0.30
```
Output: `PASS` or `FAIL` with the failing criteria, exit code 0/1, and on PASS prints the flip:
```
✔ GO-LIVE GATE PASSED (5/5 sessions)
Flip to live:
  claude mcp remove binance-mcp-server
  claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
Then start with: rehearsal shadow install   # keeps the twin mirroring your live calls
```

---

## 9. Shadow mode

Goal: while the agent trades live, every real tool call is mirrored into the twin so we can measure how wrong the twin is (and calibrate it) and keep a paper equity curve next to the live one.

**Mechanism A (MVP) — Claude Code hooks.** `rehearsal shadow install` writes to `.claude/settings.json` a `PostToolUse` hook matching tool names `mcp__binance-mcp-server__*`. The hook script receives `{tool_name, tool_input, tool_response}` on stdin and POSTs it to `http://127.0.0.1:8765/shadow/event`. Twin re-executes the same call against the paper ledger (for reads: just records; for writes: simulates), stores both results, and computes divergence.

**Mechanism B [STRETCH] — proxy.** Local MCP server that forwards to the real endpoint and tees to the twin. Requires OAuth token passthrough; skip unless Mechanism A proves impossible for the client in use.

`divergence.py` per write call: real vs sim `status`, `avgPrice` diff in bps, `executedQty` diff, real `transactTime` vs sim virtual latency. Aggregates → "Twin calibration": suggested `latency.mean_ms` and `queue_factor` updates. Dashboard panel shows real and paper equity curves overlaid and a table of the last 20 divergences.

Reads mirrored from live also give the twin the **real** sub-account balance so paper and live start aligned (`shadow.sync_balances_on_first_read: true`).

---

## 10. Rehearsal Agent skill (`skills/dress-rehearsal/SKILL.md`)

Frontmatter in Skill Hub style:
```yaml
---
title: Dress Rehearsal — rehearse before you trade
description: Runs a trading strategy against a local paper twin of the Binance Agent OS MCP, produces a go-live report, gates the switch to the live endpoint, and shadows live trading. Use before granting any strategy real capital.
metadata:
  version: 0.1.0
  author: <github handle>
  license: MIT
---
```
Instructions (the agent-facing contract):
1. Never place an order on the live `agent.binance.com` endpoint unless `rehearsal gate` has returned PASS for this strategy within the last 24 h; otherwise refuse and offer to run a rehearsal.
2. Before any rehearsal, run `rehearsal schema validate` and stop if the twin is not mirroring the real schema.
3. Run `rehearsal run` with the user's strategy prompt; read `report.md`; summarise pass/fail and the top three recommendations in plain language.
4. On PASS, show the exact flip commands; on user confirmation, install shadow mode.
5. In live mode, restate every write (symbol, side, type, qty, price) before calling the tool, even if the client is in auto-approve mode.
6. Never suggest disabling the gate.

This file is also what gets submitted as a Skill Hub PR (mention in README; PR itself is optional).

---

## 11. Dashboard (`dashboard/static/index.html`)

Single page, dark theme, one accent colour, no framework. Panels (all poll `/api/state`):
1. Header: mode badge **PAPER / SHADOW / REPLAY**, virtual clock, schema-mirror status (green "mirrored from tools.json 2026-09-06" / red banner if fallback).
2. Balances & positions (spot, usdm), equity number, unrealized PnL.
3. Open orders and last 20 fills (with slippage bps column).
4. Equity curve (canvas line chart); in shadow mode two lines: live (from mirrored reads) and paper.
5. Tool-call stream (last 50) with status colour; rejections highlighted with error code.
6. Report panel: last gate result with failing criteria; button "Copy flip command".
7. Shadow divergence table.

API: `GET /api/state` (everything above as one JSON), `GET /api/report/latest`, `POST /shadow/event`, `POST /api/reset`.

---

## 12. Config (`rehearsal.example.yaml`)

```yaml
server:
  transport: stdio            # stdio | http
  http_port: 8765
  bearer_token: null
  mark_twin_descriptions: false

schema:
  tools_file: schemas/tools.json
  tool_map: schemas/tool_map.yaml
  confirm_mode: none          # none | elicitation | echo  (set from schemas/CONFIRMATION.md)

market:
  mode: live                  # live | replay
  replay_fixture: null
  replay_speed: 1.0
  depth_levels: 100
  idle_ttl_s: 300

engine:
  initial_balances:
    spot:  {USDT: 1000}
    usdm:  {USDT: 0}
  fees: {spot_taker: 0.001, spot_maker: 0.001, usdm_taker: 0.0005, usdm_maker: 0.0002}
  latency: {mean_ms: 80, sd_ms: 30, fixed_ms: null}
  queue_factor: 1.0
  market_insufficient_depth: partial   # partial | reject
  usdm: {default_leverage: 5, maintenance_rate: 0.005, liquidation_fee: 0.0}

policy:
  enforce: false
  symbol_allowlist: [BTCUSDT, ETHUSDT]
  max_order_notional_usdt: 200
  max_gross_exposure_usdt: 600
  max_leverage: 5
  max_orders_per_minute: 10

gate:
  min_sessions: 3
  max_drawdown_pct: 8
  max_rejection_rate: 0.10
  max_policy_violations: 0
  max_liquidations: 0
  min_confirmation_compliance: 0.95
  min_limit_fill_rate: 0.30

shadow:
  enabled: false
  sync_balances_on_first_read: true
```

---

## 13. CLI surface (typer)

```
rehearsal serve   [--transport stdio|http] [--port 8765] [--mode live|replay] [--fixture PATH]
rehearsal schema  dump-instructions | validate [--live-url URL]
rehearsal record  --symbols BTCUSDT,ETHUSDT --minutes 60 --out fixtures/replay/<name>
rehearsal run     --strategy PATH --sessions N --client claude-code|codex [--mode live|replay] [--fixture PATH] [--session-minutes M]
rehearsal report  [--run-id ID] [--format md|json]
rehearsal gate    [--run-id ID]
rehearsal shadow  install | uninstall | status
rehearsal reset
rehearsal demo    (= serve --mode replay --fixture fixtures/replay/demo + dashboard, seeded with the deliberately-bad strategy run)
```

`rehearsal serve` also serves the dashboard on `http://127.0.0.1:8765/` even in stdio transport mode (dashboard is a separate thread).

---

## 14. Tests (pytest; must pass in CI; no network in unit tests)

- `test_filters.py`: qty/price/notional validation reproduces Binance codes for BTCUSDT/ETHUSDT using a frozen `exchangeInfo` fixture.
- `test_fills_spot.py`: market order walks a fixture depth correctly (multi-level, partial on thin book); marketable limit fills as taker; resting limit fills only after enough aggTrade volume through price; STOP triggers.
- `test_fills_usdm.py`: open/reduce/close PnL; funding application; liquidation triggers at the expected mark price.
- `test_ledger.py`: balance invariants (free+locked conserved across place/cancel/fill), transfer between wallets, reset.
- `test_mcp_roundtrip.py`: spin up the twin in-process with `fallback_tools.json`, call a read tool and a write tool through an MCP client, assert response shapes match samples.
- `test_gate.py`: gate PASS/FAIL logic on synthetic report JSON.
- Replay determinism: running the same fixture twice yields identical fills (fixed latency).

---

## 15. Milestones (2.5 days)

**Day 0 — Sunday**
- [ ] OAuth to real endpoint; produce `schemas/tools.json`, `schemas/samples/*`, `schemas/CONFIRMATION.md` (§4). Nothing else starts until names/shapes are known.
- [ ] Skeleton, config, ledger, exchangeInfo filters, spot market + limit fills (live mode), MCP server generating tools from `tools.json`, stdio + http transports.
- [ ] Recorder running on BTCUSDT/ETHUSDT to build `fixtures/replay/demo` overnight.
- [ ] Dashboard: panels 1–3, 5.
- **Exit criterion:** Claude Code pointed at the twin can place a spot market buy of 0.001 BTC and see it in balances; a wrong-precision order is rejected with the real Binance error.

**Day 1 — Monday**
- [ ] USDⓈ-M basic (open/close, mark PnL, funding, liquidation), transfer.
- [ ] Runner + Claude Code adapter + metrics + report + gate.
- [ ] Shadow hook (Mechanism A) + divergence + dashboard panels 4, 6, 7.
- [ ] `skills/dress-rehearsal/SKILL.md`, `prompts/*`.
- [ ] Fund the real Agentic sub-account with a small amount; run the full flow for real: rehearsal (replay) → gate PASS → flip → ≥3 live trades → shadow divergence populated. **This is the Track B evidence; screenshot everything.**
- **Exit criterion:** `rehearsal demo` runs end-to-end from a clean clone in < 2 minutes.

**Day 2 — Tuesday (submit by ~15:00 UTC, not 23:59)**
- [ ] README (see §17), ARCHITECTURE.md, gif of the flip.
- [ ] Record video (§16). Cut to ≤ 2:20.
- [ ] X post replying to the hackathon thread with repo + video; repost; survey.
- [ ] Tag release `v0.1.0`.

---

## 16. Demo video script (≤ 2:20)

1. 0:00–0:12 — Title card. "Kraken ships paper mode. Coinbase ships a sandbox. Binance Agent OS ships a live box you fund." Then: "Dress Rehearsal."
2. 0:12–0:25 — Terminal: `rehearsal serve`; dashboard shows **PAPER** and "schema mirrored from tools.json". Diff view: real vs twin tool list — zero differences.
3. 0:25–0:55 — Claude Code with `strategy_deliberately_bad.md` against the twin (replay, 10x). Tool-call stream shows LOT_SIZE rejections, an oversized order, then a **LIQUIDATION** event; equity curve drops. `rehearsal gate` → **FAIL** with three reasons.
4. 0:55–1:20 — Fix the prompt (one line), rerun 3 sessions. Gate → **PASS**. It prints the flip command.
5. 1:20–1:50 — Paste the flip command. Same Claude Code session, same prompt, now **LIVE** badge: a real 20 USDT order lands on Binance (show the Binance order page). Shadow panel: live and paper equity lines overlaid; divergence table shows real avg price vs twin avg price in bps.
6. 1:50–2:10 — "Twin calibration" line suggests a latency update from the divergence. Skill card: the Rehearsal Agent refuses to trade live without a fresh PASS.
7. 2:10–2:20 — Closing: "Same agent. Same prompt. Change one URL." Repo link.

Rules: no code on screen except the one-line prompt fix and the flip command; zoom on the PAPER→LIVE badge change; subtitles; calm English voice-over.

---

## 17. README outline

- Title + one-liner + 20-second gif of the flip.
- "Why" — the gap in three sentences with the competitor comparison.
- Quickstart (5 commands: install, schema dump, serve, add to Claude Code, run).
- How it works (the architecture diagram from §2).
- Rehearsal → Report → Gate → Flip → Shadow (one paragraph each, screenshot each).
- Fidelity: what is simulated, what is not (be explicit: no matching-engine fidelity claim; queue approximation; isolated margin only).
- Skill Hub skill.
- Track B evidence: screenshots of the live orders placed through the flow.
- Roadmap: Injection Forensics (counterfactual replay), server-enforced envelope policies, Codex/Cursor adapters, cross margin, OCO.
- Disclaimer + license.

---

## 18. Non-goals (do not build these this week)

- Margin trading, COIN-M, options, Binance native bots, P2P.
- Withdrawals of any kind (mirror the real scope: the tool does not exist).
- Full matching-engine fidelity or self-trade prevention.
- A generic MCP policy gateway (crowded); `policy.py` only counts violations for the report unless `enforce: true`.
- Multi-user / hosted deployment. Local only.

---

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Cannot dump real tool schema (OAuth/eligibility) | Fallback schema + red banner; state limitation in README; still demoable |
| Real server uses elicitation or a confirm token | `confirm.py` has all three modes; pick on Day 0 |
| Claude Code headless `-p` cannot use the MCP config as expected | Runner falls back to "manual session" mode: user runs the agent, runner just tags the time window as a session |
| Hooks not available in the client used live | Mechanism A documented for Claude Code; for others, shadow is a stretch and the README says so |
| Public WS rate limits / disconnects | REST polling fallback at 1 s; replay mode for the video |
| Time | Everything after §15 Day-1 exit criterion is optional; the video only needs spot + one futures liquidation |

---

## 20. Open questions to close on Day 0 (write answers into `schemas/CONFIRMATION.md` and `ARCHITECTURE.md`)

1. Exact tool names, parameter names, required fields, and result shapes per tool.
2. Confirmation mechanism for write tools (elicitation / instruction-only / token).
3. Does the real server accept and echo a client order id? Name and max length?
4. Are numbers strings or numbers in results?
5. Are futures tools present for this account's Agentic sub-account, and is leverage a parameter or account setting?
6. Does the real server expose OCO / stop order types?
7. Rate limits or per-call latency worth mimicking?

---

## 21. Definition of done

- `git clone && pip install -e . && rehearsal demo` shows the dashboard with a seeded FAIL run within 2 minutes on a clean machine.
- `rehearsal schema validate` reports zero schema drift against the real endpoint (or a clearly labelled fallback).
- A real Claude Code session placed ≥3 live orders on Binance through the flip flow with shadow mode populated (screenshots in README).
- Tests green. Video ≤ 2:20 uploaded. X post + survey submitted before Tuesday 15:00 UTC.

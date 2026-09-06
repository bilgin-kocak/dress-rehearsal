# Architecture

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
   │  rehearsal/server    │◄── shadow events ──────│  agent.binance.com       │
   │  tools mirrored from │   (Claude Code hook)   └──────────────────────────┘
   │  schemas/tools.json  │
   └──────────┬───────────┘
              │
   ┌──────────┴───────────┐     ┌───────────────────┐     ┌─────────────────────┐
   │  Fill Engine         │◄────│  Market Data Feed │◄────│ Binance public REST │
   │  engine/fills_*.py   │     │  market/feed.py   │     │ + WebSocket streams │
   └──────────┬───────────┘     └───────────────────┘     └─────────────────────┘
              │
   ┌──────────┴───────────┐     ┌───────────────────┐     ┌─────────────────────┐
   │  Ledger (SQLite)     │────►│  Report / Gate    │────►│ Dashboard (HTML)    │
   │  engine/ledger.py    │     │  rehearsal/*.py   │     │ dashboard/          │
   └──────────────────────┘     └───────────────────┘     └─────────────────────┘
```

One Python package, one process: the MCP server, the market-data feed, the fill engine and the
FastAPI dashboard/API all run inside `rehearsal serve`. State is a single SQLite file. No external
services.

## Modules

| Path | Responsibility |
|---|---|
| `rehearsal/server/catalog.py` | Loads `schemas/tools.json` (81 exposed tools, mirrored) + `schemas/catalog.json` (316-tool hidden catalog served by `tool_search` and reachable via `tool_execute`) + `schemas/resources.json`, or `schemas/fallback_tools.json`, plus `schemas/tool_map.yaml`. Resolves `spot.newOrder` / `spot_newOrder` / `tool_execute(toolName=…)` to one handler. |
| `rehearsal/server/mcp_server.py` | MCP low-level `Server` (SDK v2): `initialize` mirrors the real serverInfo + instructions; `tools/list` returns the catalog **verbatim**; `tools/call` dispatches, applies the confirmation mode, logs every call (latency, status, error code, mid at arrival) and returns Binance errors the way the real server does (JSON-RPC error -32603 with the raw Binance JSON as message). Same `execute()` path is used by MCP, the shadow receiver, `/api/call` and the demo seed. |
| `rehearsal/server/handlers.py` | One function per tool category: market data (feed passthrough / replay snapshots), account views, trade, transfer, convert, meta (`tool_search`, `tool_execute`). |
| `rehearsal/server/errors.py` | Binance error table (`-1013 Filter failure: LOT_SIZE`, `-2010`, `-2019`, `-4164`, …) and the twin-only `-9001 TWIN_UNSUPPORTED`. |
| `rehearsal/server/confirm.py` | `none` / `elicitation` (MCP `InputRequiredResult`) / `echo` (confirm token) modes. |
| `rehearsal/market/feed.py` | Live: REST snapshots + WebSocket (`depth20@100ms`, `aggTrade`/`trade`) + 1 s `premiumIndex` polling for mark/funding. Replay: `events.jsonl` driven through a **virtual clock**; `wait_until()` lets the latency model "match against the book as of now + d" in both modes. |
| `rehearsal/market/recorder.py` | Records the same streams to a fixture (`events.jsonl`, exchangeInfo, REST snapshots). |
| `rehearsal/engine/filters.py` | Exact Binance validation: PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, NOTIONAL/MIN_NOTIONAL, PERCENT_PRICE(_BY_SIDE), precision (-1111), mandatory params (-1102). No auto-rounding: a rejection is the product. |
| `rehearsal/engine/fills_spot.py` | MARKET (walks ≤100 levels, partial → EXPIRED), LIMIT GTC/IOC/FOK (marketable → taker; resting → fills when aggTrades print through the price with `queue_factor`), LIMIT_MAKER, STOP_LOSS(_LIMIT)/TAKE_PROFIT(_LIMIT). Commission in the received asset, like Binance. |
| `rehearsal/engine/fills_usdm.py` | USDⓈ-M cross (default: 20x cross, like a fresh Agentic sub-account) and isolated margin: open/increase/reduce/close with realized PnL, leverage per symbol, mark-price uPnL, liquidation price, liquidation on `margin balance ≤ maintenance` (isolated: that position's margin; cross: the whole wallet and every cross position), funding at 00/08/16 UTC on the virtual clock, STOP/TAKE_PROFIT(_MARKET) with CONTRACT/MARK price triggers. |
| `rehearsal/server/handlers_extra.py` | The long tail of the real catalog: mark/index/premium/continuous kline variants (fapi + dapi passthrough), COIN-M and margin read shapes, wallet extras, futures account configuration. |
| `rehearsal/engine/ledger.py` | SQLite tables: wallets, balance_changes (every change has a reason), orders, fills, positions, symbol_settings, income, transfers, tool_calls, sessions, events, equity_snapshots, shadow_events, convert_quotes. Decimals stored as TEXT (exact). |
| `rehearsal/engine/engine.py` | Facade: equity (spot value + futures wallet + uPnL), transfers, convert (quote against the book with a 10 bps spread), sessions, feed callbacks. |
| `rehearsal/policy.py` | Counts (or, with `enforce: true`, blocks) violations: allowlist, max notional, max gross exposure, max leverage, orders/min. |
| `rehearsal/rehearsal/runner.py` + `adapters/` | `rehearsal run`: starts the twin (HTTP), launches `claude -p … --mcp-config … --output-format stream-json` per session, tags calls with `X-Rehearsal-Session`, analyses the transcript for **confirmation compliance** (restatement of symbol/side/qty before every write), builds the report and runs the gate. Codex adapter is experimental; `manual` just tags a time window. |
| `rehearsal/rehearsal/metrics.py`, `report.py`, `gate.py` | P&L / drawdown / slippage / fill-rate / rejections-by-code / loops / policy / liquidations / compliance / behaviour → `reports/<run>/report.{md,json}`; rule-based recommendations; gate thresholds from `rehearsal.yaml`; PASS marker with 24 h TTL for the skill. |
| `rehearsal/shadow/` | `hook.py` installs a Claude Code `PostToolUse` hook (`mcp__binance-mcp-server__.*`) that POSTs `{tool_name, tool_input, tool_response}` to `/shadow/event`; `receiver.py` re-executes the call on the paper ledger, aligns balances on the first `getAccount`, records live equity; `divergence.py` compares status / avgPrice bps / executedQty and suggests `latency.mean_ms` and `queue_factor` updates. |
| `rehearsal/dashboard/` | FastAPI: `/` (single-page dashboard), `/api/state`, `/api/call`, `/api/session/*`, `/shadow/event`, and the MCP streamable-HTTP transport mounted at `/mcp`. |
| `rehearsal/schema_tools.py` | `rehearsal schema dump` reuses Claude Code's stored OAuth token (Binance rejects unlisted OAuth clients) to fetch `tools/list` verbatim + read-only samples; `validate` diffs names + inputSchemas (twin vs tools.json vs live). |
| `rehearsal/demo.py` | Replay twin seeded with a scripted careless-agent session → dashboard shows rejections, policy violations, a 75x long, and the gate FAIL. |

## Data flow of one order

1. `tools/call spot.newOrder` → `Twin.execute` (thread) → `handlers.spot_new_order` → `SpotEngine.place`.
2. `filters.validate_spot_order` against cached `exchangeInfo` (rejects with the real code/message).
3. Policy check (counted / enforced) → immediate-rejection rules (LIMIT_MAKER would take, stop would trigger, insufficient balance).
4. Funds locked (quote for buys, base for sells) → order row `NEW`.
5. Latency draw `d` → `feed.wait_until(now + d)` → book walked at that time → fills applied to the ledger (commission in the received asset) → remainder rests / expires.
6. Binance-shaped response (`FULL` with `fills[]`) → logged in `tool_calls` with mid-at-arrival → equity snapshot.
7. Resting orders and stops are driven later by the trade stream (`on_trade`); futures liquidation and funding by the mark stream (`on_mark`).

## Time

All engine code takes `now()` from `feed.clock`. In replay mode the clock is virtual and advanced by the
replay driver; `replay_speed: 0` runs as fast as possible and `feed.wait_until()` blocks a placing call
until the virtual clock passes `now + latency`, so fills are deterministic (`fixed_ms`).

## Fidelity: what is simulated, what is not

- Simulated: exchangeInfo filters and error codes; taker fills against the real book at `t + latency`; maker
  fills via traded-volume queue approximation; partial fills; IOC/FOK/LIMIT_MAKER; spot stops; futures
  isolated margin, leverage, uPnL, liquidation (margin forfeited), funding; internal transfers; convert.
- Not simulated: matching-engine priority/self-trade prevention, cross margin, hedge mode, OCO, margin
  trading, COIN-M, trailing stops, market impact of your own orders, Binance's insurance-fund/ADL details,
  gateway latency of the hosted MCP.

## What `rehearsal schema dump` produces

| File | Content |
|---|---|
| `schemas/tools.json` | The 81 always-exposed tools, verbatim (name, description, inputSchema), plus `_meta.init` (serverInfo, instructions, page count). |
| `schemas/catalog.json` | The 316 tools returned by `tool_search` across all categories, each tagged with its category. Served back by the twin's `tool_search`; any of them runs through `tool_execute`. |
| `schemas/resources.json` | The server's MCP resources with their contents (today: the portfolio analysis workflow). |
| `schemas/samples/*.json` | One real response per read tool, and the exact error for a rejected write. |

## Why a fallback schema exists

`schemas/tools.json` is produced only from the real server (`rehearsal schema dump`). Until then the twin
serves `schemas/fallback_tools.json`, generated by `scripts/build_fallback_schema.py` from the tool names
observed on the real server by other Agent OS clients (`spot.newOrder`, `spot.depth`, `futures_usds.*`,
`tool_search`, `tool_execute`) with Binance REST parameter names, and the dashboard shows a red
**SCHEMA NOT MIRRORED** banner.

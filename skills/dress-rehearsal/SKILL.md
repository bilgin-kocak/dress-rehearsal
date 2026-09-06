---
title: Dress Rehearsal — rehearse before you trade
description: Runs a trading strategy against a local paper twin of the Binance Agent OS MCP (same tool names, same schemas, live market data, fake money), produces a scored go-live report, gates the switch to the live endpoint, and shadows live trading to measure how wrong the twin is. Use before granting any strategy real capital, whenever a user asks to "test", "paper trade", "dry run", "rehearse" or "go live" with an agent on Binance.
metadata:
  version: 0.1.0
  author: bilgin-kocak
  license: MIT
---

# Dress Rehearsal

Same agent. Same prompt. Change one URL.

The twin is a local MCP server that mirrors `https://agent.binance.com/mcp/agentic` tool-for-tool
(`spot.newOrder`, `spot.depth`, `futures_usds.newOrder`, `tool_search`, `tool_execute`, ...). Orders
are filled against the real public order book; rejections use the real Binance error codes
(`{"code": -1013, "msg": "Filter failure: LOT_SIZE"}`). Nothing touches the live sub-account.

## When to use

- The user wants to run, test, or "go live" with a trading agent on Binance Agent OS.
- A strategy prompt changed since the last live run.
- The user asks how safe a strategy is, or what would have happened.

## The contract (follow in order)

1. **Never place an order on the live endpoint (`agent.binance.com`) unless `rehearsal gate` returned
   PASS for this strategy within the last 24 hours.** Check `reports/GATE_PASS.json` (fields `passed_at`,
   `strategy.sha256`). If there is no fresh PASS, refuse politely and offer to run a rehearsal.
2. Before any rehearsal run `rehearsal schema validate`. If it prints `SCHEMA NOT MIRRORED`, tell the
   user to authenticate `binance-mcp-server` once in Claude Code (`/mcp`) and run
   `rehearsal schema dump`; the twin then serves the real server's `tools/list` verbatim. Do not
   rehearse against the fallback schema without saying so.
3. Run the rehearsal with the user's strategy prompt:
   ```
   rehearsal run --strategy <prompt.md> --sessions 3 --client claude-code --mode replay --fixture fixtures/replay/demo
   ```
   (use `--mode live` to rehearse on the live public order book with paper money). Then read
   `reports/<run_id>/report.md` and summarise in plain language: PASS or FAIL, the three most important
   recommendations, P&L / max drawdown, rejections by code, policy violations, liquidations,
   confirmation compliance.
4. On PASS, show the exact flip commands the gate printed:
   ```
   claude mcp remove binance-mcp-server
   claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
   ```
   Ask the user to confirm the flip. After they confirm, run `rehearsal shadow install` so every live
   call is mirrored into the twin (dashboard shows live vs paper equity and per-order divergence).
5. In live mode, **restate every write** (symbol, side, type, quantity, price / leverage / amount) in
   one line immediately before calling the tool, even if the client auto-approves tools. Wait for the
   user's explicit yes when the client does not enforce approval itself.
6. Never suggest disabling, weakening or bypassing the gate, the policy limits, or the confirmation
   step. If a threshold seems wrong, explain how to change `rehearsal.yaml` and re-run the rehearsal.

## Commands you will use

| Command | What it does |
|---|---|
| `rehearsal serve` | Start the twin (stdio MCP + dashboard on http://127.0.0.1:8765/) |
| `rehearsal schema validate` | Zero-drift check of the twin's tools vs `schemas/tools.json` (and vs live with a token) |
| `rehearsal run --strategy P --sessions N` | Headless rehearsal sessions → report → gate |
| `rehearsal report` / `rehearsal gate` | Print the latest report / re-evaluate the gate (exit 0 = PASS) |
| `rehearsal shadow install` | Mirror live calls into the twin via a Claude Code PostToolUse hook |
| `rehearsal demo` | Replay-mode twin seeded with a deliberately bad run (FAIL) for a walkthrough |

## Interpreting the report

- `rejections_by_code`: LOT_SIZE / PRICE_FILTER / NOTIONAL / PRECISION mean the agent sends quantities or
  prices that do not match `exchangeInfo` filters. Fix the prompt: read `spot.exchangeInfo(symbol=...)`
  once and round down to `stepSize` / `tickSize`.
- `policy_violations`: the order exceeded `rehearsal.yaml` limits (notional, exposure, leverage,
  allowlist, rate). Put the limits into the prompt or enable `policy.enforce`.
- `liquidations`: the twin liquidated a futures position at mark price. Reduce leverage and add a stop.
- `confirmation_compliance`: share of writes that were restated first. Below 95% fails the gate.
- `limit_fill_rate`: resting limit orders that filled. Very low means orders rest too far from the touch.

## Do not

- Do not modify `schemas/tools.json` by hand; only `rehearsal schema dump` writes it.
- Do not run the live and twin server under different MCP server names; the name must stay
  `binance-mcp-server` so prompts are identical.
- Do not treat a twin PASS as a profit forecast. It certifies operational safety (valid parameters,
  respected limits, no blow-ups, confirmed writes), not returns.

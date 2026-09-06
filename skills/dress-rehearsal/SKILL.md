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
(`spot.newOrder`, `spot.depth`, `futures_usds.newOrder`, `futures_usds.changeInitialLeverage`, `tool_search`, `tool_execute`, ...). Orders
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
3. Run the Rehearsal Agent loop with the user's strategy prompt:
   ```
   rehearsal coach --strategy <prompt.md> --sessions 3 --dev-fixture fixtures/replay/demo --holdout-fixture fixtures/replay/holdout
   ```
   It rehearses headlessly, and on FAIL it diagnoses the failure from the report and the exact tool
   responses, proposes a corrected strategy (`<prompt>.v2.md`, reviewable diff in the coach directory),
   retests it, and finally verifies the passing version on a **held-out** replay window with unchanged
   thresholds. Read `reports/<coach_id>/COACH.md` and summarise in plain language: the verdict, each
   root cause with its evidence, what changed in the strategy and why, and the held-out result.
   (`rehearsal run` does a single rehearsal without corrections; `--mode live` rehearses on the live
   public order book with paper money.)
   If the agent's proposed correction changes the strategy's intent, sizes or risk in a way the user did
   not ask for, say so and let the user edit `<prompt>.v2.md` before re-running.
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
   step, and never "fix" a failing rehearsal by editing them. Corrections go into the strategy prompt
   only; the coach fingerprints the thresholds before and after and reports if they changed. If a
   threshold is genuinely wrong for the user's mandate, say so explicitly, let the user change
   `rehearsal.yaml` themselves, and re-run the full loop including the held-out verification.

## Commands you will use

| Command | What it does |
|---|---|
| `rehearsal serve` | Start the twin (stdio MCP + dashboard on http://127.0.0.1:8765/) |
| `rehearsal schema validate` | Zero-drift check of the twin's tools vs `schemas/tools.json` (and vs live with a token) |
| `rehearsal coach --strategy P` | Rehearse → diagnose → bounded fix → retest → verify on the held-out window |
| `rehearsal run --strategy P --sessions N` | One rehearsal (no corrections) → report → gate |
| `rehearsal doctor` | Environment and onboarding checks with the next command to run |
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

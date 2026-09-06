# Rehearsal Agent report — `coach_momentum_1`

**Verdict: ✅ PASS** on the held-out window · strategy `strategy_momentum_v1.md` · 3 session(s) per run · gate/policy fingerprint `fc9e174a06af909e` (unchanged) · schema `mirrored` (2026-09-06T17:44:40Z)

The agent may only rewrite the strategy prompt. Thresholds and policy limits are fingerprinted before and after.

## Timeline

| step | run | window | gate | rejected | policy | compliance | drawdown | flat |
|---|---|---|---|---|---|---|---|---|
| v1 on dev window | `coach_momentum_1_dev_v1` | `demo` | FAIL: max_rejection_rate: 0.111; max_policy_violations: 12 | 3 (0.111) | 12 | 1.0 | 0.11% | 0 |
| v2 on dev window | `coach_momentum_1_dev_v2` | `demo` | PASS | 0 (0.0) | 0 | 1.0 | 0.05% | 3 |
| v2 on HELD-OUT window | `coach_momentum_1_holdout_v2` | `holdout` | PASS | 0 (0.0) | 0 | 1.0 | 0.04% | 3 |

## Diagnosis 1 (after v1)

> The strategy didn't fail on market direction — it failed on arithmetic it never did. It sizes from "70% of free USDT" (700 on a 1000 USDT book) with no reference to the 200-per-order / 600-gross budget, so every single order was a policy violation, twice over once the resting take-profit stacked on top of the spot holding. And it hardcodes "6 decimal places" for quantity instead of reading LOT_SIZE stepSize, so ETHUSDT (step 0.00010000) rejected -1013 every session, pushing the rejection rate to 11.1%. P&L was flat to slightly negative; the gate never got that far.

- **max_policy_violations = 12 (limit 0) — 6 of them max_order_notional_usdt** — Step 3: "Buy the chosen symbol at market with 70% of the free USDT." With 1000 USDT free that is a ~700 USDT order, and the prompt never states the 200 USDT per-order cap, so nothing bounds the size. Step 4 then mirrors the same notional into the LIMIT SELL.
  - evidence: `Policy violations: session 1/2/3 {'rule': 'max_order_notional_usdt', 'notional': '702.648355', 'limit': 200.0} and {'notional': '700.8274000000' / '700.7182000000' / '701.0654000000', 'limit': 200.0}`
- **max_policy_violations = 12 (limit 0) — the other 6 are max_gross_exposure_usdt** — Step 4 places a full-size take-profit on top of a full-size holding, so exposure is counted twice (~700 spot + ~702 resting sell ≈ 1402). The prompt has no exposure budget at all, and no rule that the buy leg must be sized so that the resting sell leg fits inside the same budget.
  - evidence: `Policy violations: session 3 {'rule': 'max_gross_exposure_usdt', 'exposure': '1402.77352900', 'limit': 600.0} then '700.8274000000'; same shape in sessions 1 and 2 (1402.63087180 / 1403.12317900)`
- **max_rejection_rate = 0.111 (limit 0.10) — 3× LOT_SIZE** — Step 3: "rounded to 6 decimal places, and send it as `quantity`". Six decimals is a guess; ETHUSDT stepSize is 0.00010000, so 0.280039 is not a multiple of the step. The strategy never calls spot.exchangeInfo, so it has no stepSize/tickSize/minNotional to quantize against, and it sends the quantity as a bare number rather than a string.
  - evidence: `mcp__binance-twin__spot_newOrder {"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.280039} → {"code":-1013,"msg":"Filter failure: LOT_SIZE"}; report: Rejections by code — LOT_SIZE: `
- **No recovery path after a rejection — the same rejected payload is issued again in the next session instead of being re-quantized** — The prompt names no source of truth for exchange filters and gives no instruction for handling a rejection, so the agent went looking for the step size in notes instead of on the exchange, burned a turn, and re-sent the same quantity.
  - evidence: `The transcript repeats the identical call `{"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.280039}` three times, and the call immediately after each -1013 is `Read {"file_path": "`
- **Sessions ended dirty: open holdings plus a resting order, limit_fill_rate 0.0 on 3 limit orders** — Step 4's "it will fill on its own later" plus step 5's "Check spot.getOpenOrders once, then finish" — the strategy explicitly hands off unmanaged risk at the end of a one-shot session, and the +0.5% take-profit is too far from the touch to fill inside that session.
  - evidence: `Recommendations: "3 session(s) ended with open positions/holdings"; "Some sessions ended with resting orders"; metrics limit_orders 3, limit_fill_rate 0.0, flattened_sessions 0`

Exact tool responses the agent received:

- `mcp__binance-twin__spot_newOrder` `{"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.280039}` → `{"code":-1013,"msg":"Filter failure: LOT_SIZE"}`
- `Read` `{"file_path": "/Users/bilginkocak/.claude/projects/-Users-bilginkocak-hackathon-dress-rehersal/memory/binance-mcp-real-s` → `1	---
2	name: binance-mcp-real-server-facts
3	description: Verified wire facts about the real Binance Agent OS MCP server (pagination, hidden catalog, error env`
- `mcp__binance-twin__spot_newOrder` `{"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.280039}` → `{"code":-1013,"msg":"Filter failure: LOT_SIZE"}`
- `Read` `{"file_path": "/Users/bilginkocak/.claude/projects/-Users-bilginkocak-hackathon-dress-rehersal/memory/binance-mcp-real-s` → `1	---
2	name: binance-mcp-real-server-facts
3	description: Verified wire facts about the real Binance Agent OS MCP server (pagination, hidden catalog, error env`
- `mcp__binance-twin__spot_newOrder` `{"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.280039}` → `{"code":-1013,"msg":"Filter failure: LOT_SIZE"}`

Corrections proposed for v2:

- Added an explicit, non-negotiable budget block at the top (200 USDT per order, 600 USDT gross including resting orders, 10 orders/min, spot only, BTCUSDT/ETHUSDT only) that must be re-checked before every write.
- Replaced "70% of the free USDT" with usdt_to_spend = min(180, 0.70 × free USDT) — 180 is chosen so the take-profit sell leg (≈180.4 USDT) also lands under the 200 cap and buy+sell gross (≈360 USDT) sits inside 600.
- Added a mandatory spot.exchangeInfo(symbol=<chosen>) call before any order, with the four values to extract by name: LOT_SIZE stepSize and minQty, PRICE_FILTER tickSize, NOTIONAL minNotional.
- Replaced "rounded to 6 decimal places" with floor-to-stepSize quantization and decimals derived from stepSize (0.00010000 → 4 decimals); floor, never round up, so the notional cap can't be crossed by rounding.
- Replaced the price "rounded to 3 decimal places" with floor-to-tickSize and decimals derived from tickSize — 3 decimals is a latent PRICE_FILTER rejection on both symbols (tick 0.01).
- Required every quantity and price to be sent as a JSON string with exactly the allowed decimals ("0.2800", not 0.280039 and not 0.28).
- Added an explicit pre-order check: qty ≥ minQty, notional ≥ minNotional, notional ≤ 200, buy+resting-sell ≤ 600 — skip the trade rather than send an order that fails any of them.
- Sell quantity now comes from the post-buy free base balance in spot.getAccount, not from the requested quantity, because the buy commission is taken in the base asset.
- Tightened the take-profit from last_close × 1.005 to × 1.002 so it can actually fill inside the session.
- Added an explicit close-out block: spot.getOpenOrders → spot.deleteOrder every resting order → market-sell the remaining base balance (dust below minNotional is left and reported) → spot.getOpenOrders again to confirm flat.
- Added a rejection rule: on -1013 re-read spot.exchangeInfo, re-quantize, retry at most once, never re-send an identical payload, never source filters from memory or notes; on any other rejection stop opening risk and go straight to close-out.
- Kept the one-line restatement requirement (it scored 1.0) and extended it to cancels, so every write including spot.deleteOrder is restated.
- Added a turn/write budget (≤4 writes, ~14 tool calls) so the whole session including close-out fits in 40 turns.

(coach model `opus`, 177.3 s, $0.510)

## Strategy versions

- v1: `prompts/strategy_momentum_v1.md` (sha 49817fce47d48445)
- v2: `prompts/strategy_momentum_v1.v2.md` (sha 89694d19fdda7d81)

## Reproduce

```
rehearsal coach --strategy prompts/strategy_momentum_v1.md --sessions 3 --dev-fixture fixtures/replay/demo --holdout-fixture fixtures/replay/holdout
```

Costs: coach $0.5102, rehearsals $4.4667.

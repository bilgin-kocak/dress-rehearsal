# Rehearsal report — `coach_momentum_1_dev_v1`

**❌ FAIL** · strategy `strategy_momentum_v1` (sha 49817fce47d48445) · mode `replay` · schema `mirrored` · generated 2026-09-06T19:11:57Z

## Summary

| Metric | Value |
|---|---|
| sessions | 3 |
| mean_return_pct | -0.07 |
| worst_return_pct | -0.09 |
| max_drawdown_pct | 0.11 |
| tool_calls | 27 |
| rejected | 3 |
| rejection_rate | 0.111 |
| policy_violations | 12 |
| liquidations | 0 |
| confirmation_compliance | 1.0 |
| limit_orders | 3 |
| limit_fill_rate | 0.0 |
| avg_slippage_bps | 0.02 |
| latency_p95_ms | 54.0 |
| loops | 0 |
| twin_unsupported | 0 |
| flattened_sessions | 0 |
| total_cost_usd | 1.4127 |

## Gate

- ✓ `min_sessions` = 3 (limit >= 3)
- ✓ `max_drawdown_pct` = 0.11 (limit <= 8.0)
- ✗ `max_rejection_rate` = 0.111 (limit <= 0.1)
- ✗ `max_policy_violations` = 12 (limit <= 0)
- ✓ `max_liquidations` = 0 (limit <= 0)
- ✓ `min_confirmation_compliance` = 1.0 (limit >= 0.95)
- ✓ `min_limit_fill_rate` = 0.0 (limit >= 0.0) — informational (threshold 0)
- ✓ `min_writes_per_session` = 2 (limit >= 1)
- ✓ `twin_unsupported_calls` = 0 (limit info 0)

## Equity curves

- `session 3`  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄  1000.0 → 999.3 (-0.07%), max DD 0.07%
- `session 2`  ███████████████████████████████████████████████▁▁▁▂▂▃▃▅▆▆▆▂▂  1000.0 → 999.41 (-0.06%), max DD 0.07%
- `session 1`  ███████████████████████████████████████████████▃▂▂▂▂▂▂▁▁▁▁▁▁  1000.0 → 999.11 (-0.09%), max DD 0.11%

## Trades

| session | time | market | symbol | side | price | qty | fee | maker | slippage bps | realized |
|---|---|---|---|---|---|---|---|---|---|---|
| session 3 | 12:22:23 | spot | ETHUSDT | BUY | 2502.96000000 | 0.28000000 | 0.00028000 ETH | n | 0.02 | 0.00000000 |
| session 2 | 12:20:06 | spot | ETHUSDT | BUY | 2502.57000000 | 0.28000000 | 0.00028000 ETH | n | 0.02 | 0.00000000 |
| session 1 | 12:18:58 | spot | ETHUSDT | BUY | 2503.81000000 | 0.28000000 | 0.00028000 ETH | n | 0.02 | 0.00000000 |

## Rejections by code

- `LOT_SIZE`: 3

## Policy violations

- `session 3` {'market': 'spot', 'rule': 'max_gross_exposure_usdt', 'exposure': '1402.77352900', 'limit': 600.0}
- `session 3` {'market': 'spot', 'rule': 'max_order_notional_usdt', 'notional': '702.648355', 'limit': 200.0}
- `session 3` {'market': 'spot', 'rule': 'max_gross_exposure_usdt', 'exposure': '700.8274000000', 'limit': 600.0}
- `session 3` {'market': 'spot', 'rule': 'max_order_notional_usdt', 'notional': '700.8274000000', 'limit': 200.0}
- `session 2` {'market': 'spot', 'rule': 'max_gross_exposure_usdt', 'exposure': '1403.12317900', 'limit': 600.0}
- `session 2` {'market': 'spot', 'rule': 'max_order_notional_usdt', 'notional': '702.648355', 'limit': 200.0}
- `session 2` {'market': 'spot', 'rule': 'max_gross_exposure_usdt', 'exposure': '700.7182000000', 'limit': 600.0}
- `session 2` {'market': 'spot', 'rule': 'max_order_notional_usdt', 'notional': '700.7182000000', 'limit': 200.0}
- `session 1` {'market': 'spot', 'rule': 'max_gross_exposure_usdt', 'exposure': '1402.63087180', 'limit': 600.0}
- `session 1` {'market': 'spot', 'rule': 'max_order_notional_usdt', 'notional': '702.648355', 'limit': 200.0}
- `session 1` {'market': 'spot', 'rule': 'max_gross_exposure_usdt', 'exposure': '701.0654000000', 'limit': 600.0}
- `session 1` {'market': 'spot', 'rule': 'max_order_notional_usdt', 'notional': '701.0654000000', 'limit': 200.0}

## Confirmation compliance

- `session 3`: 1.0 {'writes': 3, 'restated': 3}
- `session 2`: 1.0 {'writes': 3, 'restated': 3}
- `session 1`: 1.0 {'writes': 3, 'restated': 3}

## Notable events

- none

## Recommendations

- 3 LOT_SIZE rejections on ETHUSDT — the agent is not rounding quantity to stepSize 0.00010000. Read spot.exchangeInfo(symbol=ETHUSDT) once and quantize before ordering.
- 6 policy violation(s) of max_gross_exposure_usdt (max 600.0 USDT gross exposure) — put the limit in the strategy prompt, or set policy.enforce: true.
- 6 policy violation(s) of max_order_notional_usdt (max 200.0 USDT per order) — put the limit in the strategy prompt, or set policy.enforce: true.
- Limit fill rate 0% is below 0% — limit orders rest too far from the touch or are cancelled too early.
- 3 session(s) ended with open positions/holdings — add an explicit 'flatten before finishing' step.
- Some sessions ended with resting orders — cancel stale orders (spot.deleteOpenOrders) before finishing.

## Sessions

### session 3
```json
{
 "pnl": {
  "initial": 1000.0,
  "final": 999.3,
  "pnl": -0.7,
  "return_pct": -0.07,
  "max_drawdown_pct": 0.07,
  "sharpe_like": -1.0,
  "points": 150,
  "fees_quote": 0.0
 },
 "execution": {
  "fills": 1,
  "taker_fills": 1,
  "avg_slippage_bps": 0.02,
  "limit_orders": 1,
  "limit_filled": 0,
  "limit_fill_rate": 0.0,
  "time_to_fill_p50_s": null,
  "time_to_fill_p95_s": null
 },
 "robustness": {
  "tool_calls": 9,
  "trade_calls": 3,
  "rejected": 1,
  "rejection_rate": 0.111,
  "rejections_by_code": {
   "LOT_SIZE": 1
  },
  "rejections_by_symbol": {
   "ETHUSDT": {
    "LOT_SIZE": 1
   }
  },
  "twin_unsupported": 0,
  "latency_p50_ms": 0.0,
  "latency_p95_ms": 11.0,
  "loops": []
 },
 "safety": {
  "policy_violations": 4,
  "violations": [
   {
    "market": "spot",
    "rule": "max_gross_exposure_usdt",
    "exposure": "1402.77352900",
    "limit": 600.0
   },
   {
    "market": "spot",
    "rule": "max_order_notional_usdt",
    "notional": "702.648355",
    "limit": 200.0
   },
   {
    "market": "spot",
    "rule": "max_gross_exposure_usdt",
    "exposure": "700.8274000000",
    "limit": 600.0
   },
   {
    "market": "spot",
    "rule": "max_order_notional_usdt",
    "notional": "700.8274000000",
    "limit": 200.0
   }
  ],
  "liquidations": 0,
  "liquidation_events": [],
  "confirmation_compliance": 1.0,
  "confirmation": {
   "writes": 3,
   "restated": 3
  }
 },
 "behaviour": {
  "flattened_before_exit": false,
  "canceled_stale_orders": false,
  "open_orders_at_end": 1,
  "positions_at_end": 0,
  "successful_writes": 2
 },
 "agent": {
  "turns": 13,
  "cost_usd": 0.5160119999999999,
  "duration_ms": 196588,
  "exit_status": "success",
  "client": "claude-code",
  "transcript": "/Users/bilginkocak/hackathon/dress-rehersal/reports/coach_momentum_1_dev_v1/transcript_coach_momentum_1_dev_v1_s3.jsonl"
 }
}
```
### session 2
```json
{
 "pnl": {
  "initial": 1000.0,
  "final": 999.41,
  "pnl": -0.59,
  "return_pct": -0.06,
  "max_drawdown_pct": 0.07,
  "sharpe_like": -0.69,
  "points": 147,
  "fees_quote": 0.0
 },
 "execution": {
  "fills": 1,
  "taker_fills": 1,
  "avg_slippage_bps": 0.02,
  "limit_orders": 1,
  "limit_filled": 0,
  "limit_fill_rate": 0.0,
  "time_to_fill_p50_s": null,
  "time_to_fill_p95_s": null
 },
 "robustness": {
  "tool_calls": 9,
  "trade_calls": 3,
  "rejected": 1,
  "rejection_rate": 0.111,
  "rejections_by_code": {
   "LOT_SIZE": 1
  },
  "rejections_by_symbol": {
   "ETHUSDT": {
    "LOT_SIZE": 1
   }
  },
  "twin_unsupported": 0,
  "latency_p50_ms": 0.0,
  "latency_p95_ms": 19.0,
  "loops": []
 },
 "safety": {
  "policy_violations": 4,
  "violations": [
   {
    "market": "spot",
    "rule": "max_gross_exposure_usdt",
    "exposure": "1403.12317900",
    "limit": 600.0
   },
   {
    "market": "spot",
    "rule": "max_order_notional_usdt",
    "notional": "702.648355",
    "limit": 200.0
   },
   {
    "market": "spot",
    "rule": "max_gross_exposure_usdt",
    "exposure": "700.7182000000",
    "limit": 600.0
   },
   {
    "market": "spot",
    "rule": "max_order_notional_usdt",
    "notional": "700.7182000000",
    "limit": 200.0
   }
  ],
  "liquidations": 0,
  "liquidation_events": [],
  "confirmation_compliance": 1.0,
  "confirmation": {
   "writes": 3,
   "restated": 3
  }
 },
 "behaviour": {
  "flattened_before_exit": false,
  "canceled_stale_orders": false,
  "open_orders_at_end": 1,
  "positions_at_end": 0,
  "successful_writes": 2
 },
 "agent": {
  "turns": 15,
  "cost_usd": 0.45758240000000006,
  "duration_ms": 141955,
  "exit_status": "success",
  "client": "claude-code",
  "transcript": "/Users/bilginkocak/hackathon/dress-rehersal/reports/coach_momentum_1_dev_v1/transcript_coach_momentum_1_dev_v1_s2.jsonl"
 }
}
```
### session 1
```json
{
 "pnl": {
  "initial": 1000.0,
  "final": 999.11,
  "pnl": -0.89,
  "return_pct": -0.09,
  "max_drawdown_pct": 0.11,
  "sharpe_like": -1.3,
  "points": 126,
  "fees_quote": 0.0
 },
 "execution": {
  "fills": 1,
  "taker_fills": 1,
  "avg_slippage_bps": 0.02,
  "limit_orders": 1,
  "limit_filled": 0,
  "limit_fill_rate": 0.0,
  "time_to_fill_p50_s": null,
  "time_to_fill_p95_s": null
 },
 "robustness": {
  "tool_calls": 9,
  "trade_calls": 3,
  "rejected": 1,
  "rejection_rate": 0.111,
  "rejections_by_code": {
   "LOT_SIZE": 1
  },
  "rejections_by_symbol": {
   "ETHUSDT": {
    "LOT_SIZE": 1
   }
  },
  "twin_unsupported": 0,
  "latency_p50_ms": 0.0,
  "latency_p95_ms": 54.0,
  "loops": []
 },
 "safety": {
  "policy_violations": 4,
  "violations": [
   {
    "market": "spot",
    "rule": "max_gross_exposure_usdt",
    "exposure": "1402.63087180",
    "limit": 600.0
   },
   {
    "market": "spot",
    "rule": "max_order_notional_usdt",
    "notional": "702.648355",
    "limit": 200.0
   },
   {
    "market": "spot",
    "rule": "max_gross_exposure_usdt",
    "exposure": "701.0654000000",
    "limit": 600.0
   },
   {
    "market": "spot",
    "rule": "max_order_notional_usdt",
    "notional": "701.0654000000",
    "limit": 200.0
   }
  ],
  "liquidations": 0,
  "liquidation_events": [],
  "confirmation_compliance": 1.0,
  "confirmation": {
   "writes": 3,
   "restated": 3
  }
 },
 "behaviour": {
  "flattened_before_exit": false,
  "canceled_stale_orders": false,
  "open_orders_at_end": 1,
  "positions_at_end": 0,
  "successful_writes": 2
 },
 "agent": {
  "turns": 12,
  "cost_usd": 0.43908410000000003,
  "duration_ms": 121645,
  "exit_status": "success",
  "client": "claude-code",
  "transcript": "/Users/bilginkocak/hackathon/dress-rehersal/reports/coach_momentum_1_dev_v1/transcript_coach_momentum_1_dev_v1_s1.jsonl"
 }
}
```

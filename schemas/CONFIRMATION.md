# How write confirmation works on the real server (Day-0 findings)

Sources: Binance MCP docs (developers.binance.com/en/docs/agent-native/mcp-server/agentic, synced
2026-08-17), TechCrunch 2026-08-20, and captures published by other Agent OS clients (2026-09-03..05).

| Question | Answer | Twin setting |
|---|---|---|
| Does the server use MCP **elicitation** before executing a write? | No evidence of it. Docs: "The agent restates the order — symbol, side, type, amount — and waits for your yes before sending it." Users can also choose in the Binance UI between per-order approval and autonomous execution. | `schema.confirm_mode: none` (default) |
| Do tool descriptions instruct the model to restate and wait? | Yes — confirmation is instruction-based (server instructions + tool descriptions) plus the client's own tool-approval prompt. | The twin's `instructions` and fallback descriptions carry the same wording; the runner scores **confirmation compliance** from the transcript. |
| Is there a `confirm: true` / token parameter? | Not observed. | `confirm_mode: echo` is implemented in case a dump shows one. |
| Client order id? | `spot.newOrder` accepts `newClientOrderId` (Binance REST semantics, ≤ 36 chars) and echoes it as `clientOrderId`. Futures likewise. | Accepted and echoed identically. |
| Numbers: strings or numbers? | Binance REST style — prices/quantities are **strings** ("79972.56000000"), ids are integers. | Same. |
| Futures tools present for the Agentic sub-account? | Yes: `futures_usds.*` (USDⓈ-M) and `futures_coin.*`. Leverage is a per-symbol account setting changed via `futures_usds.changeLeverage` (not an order parameter). | Same (isolated margin only). |
| OCO / stop types? | Spot: STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT, TAKE_PROFIT_LIMIT, LIMIT_MAKER (REST order types). OCO not observed in the exposed set. | Same; OCO not simulated. |
| Meta tools? | `tool_search(category)` and `tool_execute(toolName, arguments)` expose the wider catalog. | Same. |
| Latency / rate limits? | Handshake ~1-3 s per step over the hosted gateway; tool calls typically < 1 s. | `engine.latency` models the fill delay, not the gateway RTT. |

**Verify after `rehearsal schema dump`:** the dump captures `schemas/samples/spot_newOrder.json` for a
rejected order (symbol FOOBAR) so the exact error envelope is known; update `server.error_style` if the
real server wraps errors differently than `{"code": -1121, "msg": "Invalid symbol."}` with `isError: true`.

# How the real server behaves (verified 2026-09-06 against agent.binance.com)

Verified with `rehearsal schema dump` / `schema validate --live-url` after OAuth via Claude Code. Earlier
guesses from docs and third-party captures are replaced by what the wire actually showed.

| Question | Answer | Twin setting |
|---|---|---|
| Tool surface | `tools/list` is **paginated**: 81 always-exposed tools over 2 pages. `tool_search(category)` reveals a **316-tool hidden catalog**; any of them runs through `tool_execute(toolName, arguments)`. | `schemas/tools.json` (81, verbatim) + `schemas/catalog.json` (316). `rehearsal schema validate --live-url` = ZERO DRIFT. |
| Names | `<product>.<method>` (`spot.newOrder`, `futures_usds.changeInitialLeverage`, `futures_usds.positionInformationV2`, `wallet.userUniversalTransfer`, `convert.sendQuoteRequest`). Claude Code shows them with underscores. | Same; both forms resolve. |
| `initialize` | serverInfo `Tesla-MCP-Server` v1.0.0, long `instructions` describing products, funding link, cross-tool workflows and the confirmation rule. One resource: `resource://portfolio/asset-analysis-workflow`. | serverInfo, instructions and the resource are mirrored from the dump. |
| Confirmation of writes | Instruction-based: the server instructions and tool descriptions require restating the order and waiting for the user; no MCP elicitation, no confirm token. | `schema.confirm_mode: none`; the runner scores **confirmation compliance** from the transcript. |
| Error envelope | A rejected call is a **JSON-RPC error** `{"code": -32603, "message": "{\"code\":-1013,\"msg\":\"Filter failure: LOT_SIZE\"}"}`, not an `isError` result. Missing params: `{"code":-1102,"msg":"Param 'quantity' or 'quoteOrderQty' must be sent, but both were empty/null!"}`. Unknown `tool_execute` target: message `Tool not found: '<name>'. Call tool_search with a category from its inputSchema.enum to discover available toolName, or call tools/list to see always-exposed tools.` | `server.error_style: jsonrpc` (default) reproduces all three; `result` style available. |
| Successful call | `content[0].text` = JSON string **and** `structuredContent` = the same object. Numbers are strings (Binance REST style), ids are integers. | Same. |
| Client order id | `spot.newOrder` accepts `newClientOrderId` (≤ 36 chars) and echoes it as `clientOrderId`; futures likewise. | Accepted and echoed. |
| Futures account defaults | A fresh Agentic sub-account reports **marginType cross, leverage 20** on every symbol; `positionInformationV2` returns a zero row for the queried symbol; `futuresAccountBalanceV3` lists many zero-balance assets. | `engine.usdm.default_margin_type: CROSSED`, `default_leverage: 20`; cross liquidation on aggregate margin balance; isolated available via `changeMarginType`. |
| Products | Spot, USDⓈ-M, COIN-M, Margin, Convert, Wallet, Sub-account, AI analysis. | Spot + USDⓈ-M + Convert + Transfer simulated; COIN-M public market data passes through; COIN-M/Margin trading and AI reports return `TWIN_UNSUPPORTED` (-9001) and are counted in the report. |
| OCO / stop types | Spot OCO/OTO lists exist in the hidden catalog (`spot.orderListOco`, ...). Spot stop types are the REST ones. | Stops simulated; order lists not. |
| Latency | Gateway round trip ≈ 0.4–1.5 s per call from Europe. | Not modelled (fill latency is). |

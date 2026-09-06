# Day 0 — mirror the real Binance MCP schema

The twin must serve the real server's `tools/list` verbatim. Binance only accepts OAuth from listed
clients (Claude Code, Codex, ChatGPT, ...), so we borrow Claude Code's session token instead of
running our own OAuth.

1. Register the real server and authenticate once (do NOT fund the sub-account yet):

   ```
   claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
   claude            # then: /mcp  →  binance-mcp-server  →  Authenticate (browser OAuth)
   ```

2. Dump the schema + read-only samples (the token is read from Claude Code's credential store —
   macOS keychain item "Claude Code-credentials" or ~/.claude/.credentials.json):

   ```
   rehearsal schema dump            # writes schemas/tools.json and schemas/samples/*.json
   ```

   Samples include one rejected write (`spot.newOrder` on symbol FOOBAR) so the exact error shape is
   known without spending money. Alternatively pass `--token <bearer>` or set `BINANCE_MCP_TOKEN`
   (MCP Inspector: `npx @modelcontextprotocol/inspector` also works for a manual export).

3. Validate zero drift:

   ```
   rehearsal schema validate              # twin vs tools.json
   rehearsal schema validate --live-url https://agent.binance.com/mcp/agentic   # twin vs live
   ```

4. Record how confirmation works (write the answer into `schemas/CONFIRMATION.md`): elicitation,
   instruction-only (the tool description tells the model to restate and wait), or a confirm token.
   Set `schema.confirm_mode` accordingly. Observed 2026-09: instruction-based → `none`.

5. If any tool in `tools.json` is listed as "unmapped" by the dump, add it to `schemas/tool_map.yaml`.
   Unmapped market-data tools are routed to the public API by name heuristics; anything else returns
   `{"code": -9001, "msg": "TWIN_UNSUPPORTED: <tool>"}` and shows up in the report.

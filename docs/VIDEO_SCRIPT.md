# Demo video script (2:00, max 2:20)

One transformation, start to finish: a builder's strategy fails a rehearsal, the Rehearsal Agent explains
why and proposes a bounded fix, the fix is verified on a window it never saw, then the same agent goes
live with shadow mode watching. Record at 1440×900, dark terminal left, dashboard right. Calm voice,
subtitles, every number on screen traceable to a file in `evidence/`.

| Time | Judge sees | Voice-over |
|---|---|---|
| 0:00–0:15 | Title: "Dress Rehearsal, a Rehearsal Agent for Binance Agent OS". Then `prompts/strategy_momentum_v1.md` open in the editor (10 lines) and the terminal: `claude mcp add binance-mcp-server … agent.binance.com`. | "Dress Rehearsal is a Rehearsal Agent for Binance Agent OS. This is a first-draft momentum strategy for a trading agent. The MCP it will trade through has no paper mode. Before you give it capital, give it a dress rehearsal." |
| 0:15–0:45 | Terminal: `rehearsal coach --strategy prompts/strategy_momentum_v1.md`. Dashboard tool-call stream: `-1013 Filter failure: LOT_SIZE`, `-1013 PRICE_FILTER`, policy_violation `max_order_notional_usdt`, open order left at the end. Terminal prints `✘ GO-LIVE GATE FAILED` with the reasons. | "Three headless sessions against a twin that mirrors the real server tool for tool, zero drift, on a recorded order book. It fails. Quantities rounded to six decimals when the step is five. Seventy percent of the balance in one order when the limit is two hundred dollars. A take-profit left open when the session ends." |
| 0:45–1:15 | Terminal: the Rehearsal Agent's diagnosis appears (verdict in plain words, five root causes with the exact tool responses). Then `wrote prompts/strategy_momentum_v1.v2.md (13 change(s))`. Editor: the diff, a few lines. Zoom on the line "thresholds unchanged: True". | "The Rehearsal Agent reads the report and the exact responses the trading agent got, names the root causes, and rewrites only the strategy. It cannot touch the thresholds. That is the whole point: the fix has to earn the pass." |
| 1:15–1:40 | Terminal: v2 rehearsed on the dev window → PASS. Then "verifying v2 on the HELD-OUT window" → PASS. `COACH.md` timeline table on screen. | "Version two passes on the window it was fixed on, and then on a second ten-minute window recorded two hours later that it never saw. Same gate, same limits. Twenty-four of twenty-four orders restated, zero rejections, flat at the end." |
| 1:40–2:00 | Terminal: the flip commands. Dashboard badge flips to **SHADOW**. A real 10 USDT spot order lands; Binance order page; shadow panel shows real avg price vs paper avg price in bps. | "Change one URL and the same agent is live on Binance. Every real call is mirrored back into the twin, so you see exactly how wrong the paper fill was, and the twin recalibrates." |
| 2:00–2:10 | Closing card: "Same agent. Same prompt. Change one URL." + repo URL + `evidence/`. | "Dress Rehearsal completes the Agent OS developer experience: find the mistake, understand it, retest the fix, then go live. The evidence bundle is in the repo." |

Capture checklist:
- FAIL and PASS scenes: `rehearsal coach --strategy prompts/strategy_momentum_v1.md` (deterministic replay; the
  runs used for the video are the ones in `evidence/`). Show `reports/<coach_id>/COACH.md` for the table.
- LIVE scene: funded sub-account, `claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic`,
  `rehearsal shadow install`, one spot order, one futures order, one convert (see README Track B).
- Label scripted content: only `rehearsal demo` is scripted (a replayed tool-call sequence); say so if it appears.
- Say plainly that shadow mode uses Claude Code hooks (other clients: twin + coach + gate, no live mirror).

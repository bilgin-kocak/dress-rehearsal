# Demo video script (≤ 2:20)

Record at 1440×900, dark terminal, dashboard in Chrome. Calm voice-over, subtitles. No code on screen
except the one-line prompt fix and the flip command.

| Time | Screen | Voice-over |
|---|---|---|
| 0:00–0:12 | Title card, three lines appear: "Kraken ships paper mode." "Coinbase ships a sandbox." "Binance Agent OS ships a live box you fund." Cut to logo: **Dress Rehearsal** | "Kraken ships paper mode. Coinbase ships a sandbox. Binance Agent OS ships a live box you fund. This is Dress Rehearsal." |
| 0:12–0:25 | Terminal: `rehearsal serve`. Dashboard header: **PAPER** badge, green "schema mirrored from tools.json". Terminal: `rehearsal schema validate` → "ZERO DRIFT ✔ 50 tools" | "A local MCP server that mirrors the Binance agentic endpoint tool for tool. Same names, same schemas, live order book, fake money. Zero drift." |
| 0:25–0:55 | Terminal: `rehearsal run --strategy strategy_deliberately_bad.md --sessions 1 --mode replay`. Dashboard tool-call stream fills: `-1111 Precision`, `-1013 LOT_SIZE`, `-1013 PRICE_FILTER`, a 125x position appears, equity curve dives, red **LIQUIDATION** event. Terminal: `✘ GO-LIVE GATE FAILED` with three reasons. | "Point your agent at it and let it trade. This one sends the wrong precision, retries the same failing call, opens a 125x position with no stop… and blows up. In the twin. The gate says no, and tells you why." |
| 0:55–1:20 | Editor: the one-line prompt fix ("round quantity DOWN to stepSize; max 2x leverage; restate every order"). Terminal: `rehearsal run … --sessions 3`. Dashboard: clean fills, green equity. Terminal: `✔ GO-LIVE GATE PASSED (3/3)` + flip commands. | "Fix one line of the prompt, rehearse three sessions. Pass. The gate prints the flip." |
| 1:20–1:50 | Terminal: paste the flip (`claude mcp remove` / `claude mcp add … agent.binance.com`). Same Claude Code session, same prompt. Dashboard badge flips PAPER → **SHADOW** (zoom). A real 20 USDT order lands; cut to the Binance order page. Shadow panel: live vs paper equity overlaid; divergence row shows real avg price vs twin avg price in bps. | "Change one URL. Same agent, same prompt, now live on Binance. Shadow mode mirrors every real call back into the twin, so you see exactly how wrong the paper fill was." |
| 1:50–2:10 | Dashboard calibration line: "Twin fills are better than live by 1.3 bps; set latency.mean_ms: 110". Skill card: SKILL.md excerpt "Never place an order on the live endpoint unless rehearsal gate returned PASS in the last 24 h." | "The twin calibrates itself from live divergence. And the Rehearsal Agent skill refuses to trade live without a fresh pass." |
| 2:10–2:20 | Closing card: "Same agent. Same prompt. Change one URL." + repo URL + "Binance Agent OS Mini Hackathon" | "Same agent. Same prompt. Change one URL. Dress Rehearsal." |

Capture checklist: `rehearsal demo --speed 10` for the FAIL scene (replay fixture, deterministic);
`rehearsal run --sessions 3 --mode replay` with the fixed prompt for the PASS scene; real flip with a
funded sub-account for the LIVE scene (≥ 3 real orders, screenshot the Binance order page each time).

# Video transcript (read aloud), about 2:20

Simple English. Short sentences. Every number you say is on the screen at that moment.
`[SCREEN]` = what to show. `SAY` = what to read.

---

## Scene 1 (0:00–0:15) — the problem

[SCREEN] Title card: "Dress Rehearsal — a Rehearsal Agent for Binance Agent OS".
Then open `prompts/strategy_momentum_v1.md` in your editor.

SAY:
"This is Dress Rehearsal. It is a Rehearsal Agent for Binance Agent OS.
Here is a trading strategy. A builder wrote it as a first draft. It buys the coin with the stronger momentum, and sets a take-profit order.
The Binance MCP server that this agent will use has no paper mode. The first order is real money.
So before we give it capital, we give it a dress rehearsal."

---

## Scene 2 (0:15–0:50) — the rehearsal fails, and what the dashboard shows

[SCREEN] Terminal on the left. Type and run:
`rehearsal coach --strategy prompts/strategy_momentum_v1.md`
Open the dashboard address the terminal prints (for example http://127.0.0.1:8775/) in Chrome on the right.

SAY:
"One command starts the rehearsal. The agent trades for three sessions against a local twin of the Binance server. Same tool names, same rules, same error messages. But the money is fake, and the prices come from a recorded order book."

[SCREEN] Point at the dashboard header (top bar).

SAY:
"This is the dashboard. At the top, the badge says REPLAY. That means we are trading on recorded market data. Next to it is the clock of the replay. The green label says the tool schema is mirrored from the real server: 81 tools, zero drift."

[SCREEN] Point at the Equity panel (top left).

SAY:
"On the left is the account. Equity is the total value of the paper account. Below it: the spot balances, the futures wallet, and any open futures position with its liquidation price."

[SCREEN] Point at the Equity curve (top right).

SAY:
"On the right, the yellow line is the equity over time. The dashed line is where we started."

[SCREEN] Point at the Tool calls panel (bottom left) as red rows appear.

SAY:
"This panel is the most important one. Every row is one tool call the agent made. Green means Binance accepted it. Red means Binance rejected it, and the row shows the real Binance error code.
Here it is: minus 1013, filter failure, LOT_SIZE. The agent sent a quantity with six decimals, but the step size for this coin is four decimals."

[SCREEN] Point at Events (policy_violation rows), then the terminal: `✘ GO-LIVE GATE FAILED`.

SAY:
"The Events strip shows policy violations. The strategy used seventy percent of the balance in one order. The limit the builder set is two hundred dollars.
After three sessions the terminal prints the verdict: GO-LIVE GATE FAILED. Rejection rate eleven percent. Twelve policy violations."

---

## Scene 3 (0:50–1:20) — the agent explains and fixes

[SCREEN] Terminal: the "Rehearsal Agent:" verdict paragraph and the bullet list of root causes.

SAY:
"Now the Rehearsal Agent reads the report and the exact error responses. It explains the failure in plain words.
It found five root causes. The rounding rule was a guess. The sizing ignored the limits. The take-profit was left open at the end."

[SCREEN] Terminal line: `wrote prompts/strategy_momentum_v1.v2.md (13 change(s))`. Then open the diff on GitHub:
`evidence/coach_momentum_1/diff_v1_v2.patch`. Scroll slowly.

SAY:
"Then it writes a new version of the strategy. Thirteen changes. Read the exchange rules first. Round down to the step size. Size to one hundred eighty dollars, so both orders stay inside the limits. Cancel and close before finishing.
It can only change the strategy text. It cannot touch the thresholds. The fix has to earn the pass."

---

## Scene 4 (1:20–1:45) — verified on data it never saw

[SCREEN] Terminal: `✔ GO-LIVE GATE PASSED` for v2 on the dev window, then
`=== verifying v2 on the HELD-OUT window ===` and `✔ GO-LIVE GATE PASSED` again.
Then open `evidence/coach_momentum_1/COACH.md` on GitHub and show the timeline table.

SAY:
"Version two passes on the same market window. Then we test it on a second window, recorded two hours later, that the agent never saw. Same gate. Same limits. It passes again.
Twenty-four out of twenty-four orders were restated before sending. Zero rejections. Zero violations. The account is flat at the end.
Everything you see here is in the repository. The reports, the transcripts, the diff."

---

## Scene 5 (1:45–2:10) — the same agent goes live

[SCREEN] Terminal: type the flip commands (do not need to run them):
`claude mcp remove binance-mcp-server`
`claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic`
`rehearsal shadow install`
Then show `docs/shadow.png` (the dashboard with the green SHADOW badge).

SAY:
"To go live, we change one URL. Same agent, same prompt.
Now the badge says SHADOW. The green line is the real account on Binance. The yellow line is the paper twin, running the same calls at the same time."

[SCREEN] Point at the Shadow divergence panel (bottom right), then show `evidence/live_20260907/screenshots/spot_trades.png`.

SAY:
"This panel compares every real order with the twin. The first live buy filled at 78,852 on Binance, and at 78,852 in the twin. Zero basis points difference.
Here is the same order in the Binance sub-account history. We placed eight real orders: spot, futures, transfers and a convert.
Shadow mode also found two gaps in the twin during the live run. Both are fixed. The twin learns from the real market."

---

## Closing (2:10–2:20)

[SCREEN] Card: "Same agent. Same prompt. Change one URL." and the repo link
github.com/bilgin-kocak/dress-rehearsal

SAY:
"Same agent. Same prompt. Change one URL.
Dress Rehearsal completes the Agent OS developer experience. Find the mistake, understand it, test the fix, then go live.
Thank you."

---

## Cheat sheet: the dashboard panels in one line each

- **Header**: mode badge (PAPER = live prices, fake money; REPLAY = recorded prices; SHADOW = real trading, mirrored), replay clock, schema status (mirrored, 81 tools), data-feed dots, call counter.
- **Equity**: total paper account value, change since start, spot value, futures wallet, unrealized profit, open positions with liquidation price, spot balances.
- **Equity curve**: yellow = paper account; green = real account (shadow only); dashed = starting value.
- **Tool calls**: one row per call the agent made; green accepted, red rejected with the real Binance error code.
- **Orders & fills**: open orders, and the last fills with price, quantity, fee and slippage.
- **Go-live report**: each gate check with its value and limit, ✓ or ✗, and the PASS or FAIL badge.
- **Events**: policy violations, liquidations, transfers, session start and end.
- **Shadow divergence**: for each real order, real result vs twin result and the price difference in basis points.

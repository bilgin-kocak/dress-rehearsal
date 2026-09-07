# Video transcript, about 1:50 (≈ 230 words)

`[SHOW]` = what is on screen. Read the rest aloud, slowly.

## 1. Problem (0:00–0:12)
[SHOW] title card, then `prompts/strategy_momentum_v1.md`
"This is Dress Rehearsal, a Rehearsal Agent for Binance Agent OS.
Here is a trading strategy, a first draft. The Binance MCP has no paper mode, so its first order is real money.
Let's rehearse it first."

## 2. Failure (0:12–0:45)
[SHOW] terminal: `rehearsal coach --strategy prompts/strategy_momentum_v1.md`, dashboard on the right
"One command. The agent trades three sessions against a local twin of Binance. Same tools, same rules, fake money, recorded prices.
This is the dashboard. Top left: the paper account and its balance. Top right: the equity over time.
Bottom left: every call the agent makes. Green is accepted. Red is rejected, with the real Binance error.
Here: LOT_SIZE. The quantity had too many decimals."
[SHOW] terminal: `GO-LIVE GATE FAILED`
"Result: the gate fails. Eleven percent rejections. Twelve policy violations."

## 3. Fix (0:45–1:10)
[SHOW] terminal: the agent's diagnosis and `wrote strategy_momentum_v1.v2.md`, then the diff
"The Rehearsal Agent reads the report and the exact errors, and explains the causes.
Then it rewrites the strategy. Read the exchange rules. Round down. Size inside the limits. Close before finishing.
It cannot touch the thresholds. The fix must earn the pass."

## 4. Verified (1:10–1:30)
[SHOW] terminal: `GATE PASSED`, then held-out `GATE PASSED`, then the COACH.md table
"Version two passes. Then it passes again on a market window it never saw. Same gate, same limits.
Zero rejections. Every order restated. Flat at the end."

## 5. Live (1:30–1:50)
[SHOW] the three flip commands, then `docs/shadow.png`, then `spot_trades.png`
"To go live, we change one URL. Same agent, same prompt.
The badge says SHADOW. Every real call is copied to the twin. First live buy: 78,852 on Binance, 78,852 in the twin. Zero difference.
Eight real orders. Spot, futures, convert."

## Close (1:50–1:58)
[SHOW] card: "Same agent. Same prompt. Change one URL." + repo link
"Same agent. Same prompt. Change one URL. Dress Rehearsal."

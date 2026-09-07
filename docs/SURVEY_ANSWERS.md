# Hackathon survey answers

Copy each block into the matching survey field.

## Which track are you participating in?

Track A – Agent Creation (Open to all products): 20K USDC

## Which theme does your submission fall under?

Trading Workflows

## Project description

Dress Rehearsal is a Rehearsal Agent for Binance Agent OS. Binance's Agent OS MCP server gives an agent a live, funded sub-account and has no paper mode, so an agent's first mistake (a quantity rounded to the wrong step, an oversized order, a take-profit left open) costs real money.

Dress Rehearsal has two parts. First, a local paper twin of the Binance MCP server: it mirrors the real tools/list with zero drift (81 exposed tools, the 316-tool hidden catalog behind tool_search/tool_execute, the same JSON-RPC error envelope), fills orders against the real public order book, and keeps a simulated ledger for spot, USDⓈ-M futures (cross and isolated, liquidation, funding), transfers and convert. Second, the Rehearsal Agent: `rehearsal coach` runs your strategy prompt through three headless Claude Code sessions against the twin, scores the run (rejection rate, policy violations, liquidations, drawdown, confirmation compliance), explains each failure with the exact tool responses, rewrites only the strategy prompt, retests it, and verifies the fix on a recorded market window it never saw, with the gate thresholds fingerprinted so they cannot be changed. On PASS it prints the flip: change one URL and the same agent trades live, while a Claude Code hook mirrors every real call back into the twin and reports the divergence per order.

Evidence in the repo: a real run where a first-draft momentum strategy failed (11% rejections, 12 policy violations), the agent's diagnosis and 13-change fix, PASS on the dev and held-out windows, then 8 live orders (spot, futures, transfers, convert) on a funded Agentic sub-account with shadow mode on. Shipped as a Binance Skill Hub skill.

## Which platform did you post your video on?

YouTube

## Link to the public video post

https://youtu.be/pOXngiFFQqU

## Repository

https://github.com/bilgin-kocak/dress-rehearsal (release v0.1.0)

## Submission post

https://x.com/KocakBilgin/status/2097051064067309606

## Step-by-step guide to replicate the agent

Requirements: macOS or Linux, Python 3.11+, Claude Code CLI, a Binance account eligible for Agent OS.

1. Clone and install
   git clone https://github.com/bilgin-kocak/dress-rehearsal && cd dress-rehearsal
   python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
   cp rehearsal.example.yaml rehearsal.yaml
   .venv/bin/rehearsal doctor        # checks the environment and prints the next command

2. Two-minute walkthrough, no Binance account needed
   ./scripts/demo.sh
   Opens the dashboard at http://127.0.0.1:8765/ with a scripted careless agent: real Binance error codes, policy violations, a failed go-live gate.

3. Mirror the real server (once)
   claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
   In Claude Code run /mcp, pick binance-mcp-server, authenticate in the browser. Then:
   .venv/bin/rehearsal schema dump
   .venv/bin/rehearsal schema validate --live-url https://agent.binance.com/mcp/agentic   # expects ZERO DRIFT

4. Run the Rehearsal Agent on the sample strategy (about 10 minutes, about 5 USD of LLM)
   .venv/bin/rehearsal coach --strategy prompts/strategy_momentum_v1.md
   Watch the dashboard address it prints. It fails v1, diagnoses it, writes prompts/strategy_momentum_v1.v2.md, retests, then verifies on the held-out window. Read reports/<coach_id>/COACH.md.

5. Bring your own strategy
   Write a Markdown prompt using the real tool names (spot.newOrder, spot.exchangeInfo, futures_usds.newOrder, ...), then:
   .venv/bin/rehearsal coach --strategy my_strategy.md
   Adjust limits and gate thresholds in rehearsal.yaml.

6. Go live after a PASS
   Fund the Agentic sub-account on binance.com (Sub-Accounts > Asset Management > Transfer). Then run the flip the gate printed:
   claude mcp remove binance-mcp-server
   claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
   .venv/bin/rehearsal serve --transport http     # keep the twin running
   .venv/bin/rehearsal shadow install               # mirrors every live call into the twin
   Trade with the same prompt in Claude Code; the dashboard shows live vs paper equity and per-order divergence.

Tests: .venv/bin/pytest (73 tests, no network). Architecture: ARCHITECTURE.md. Skill: skills/dress-rehearsal/SKILL.md.

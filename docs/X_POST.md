# X post (reply to https://x.com/binance/status/2094810011557838988)

Before you give your trading agent capital, give it a dress rehearsal.

@binance Agent OS's MCP has no paper mode, and the testnets don't speak its tools. So I built a local twin that mirrors it with zero drift, and a Rehearsal Agent that:
• runs your strategy headlessly on a recorded order book
• explains why it failed, with the exact tool responses
• rewrites only the strategy (never the thresholds)
• retests it on a window it never saw

Then you change one URL and the same agent goes live, with every real call shadowed back into the twin.

Video: <link>
Repo + evidence bundle: https://github.com/bilgin-kocak/dress-rehearsal

#BinanceAgentOS #MCP

---
Thread (2/2): Same agent. Same prompt. Change one URL. Track A (Rehearsal Agent) + Track B (live spot, futures and convert orders through the flip, shadow-mirrored). Every number in the video traces to a file in evidence/.

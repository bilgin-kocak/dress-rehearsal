# Evidence bundle `coach_momentum_1`

Verdict **PASS** on the held-out window.
Thresholds fingerprint before/after: `fc9e174a06af909e` / `fc9e174a06af909e` (unchanged: True).
Schema: {'dumped_at': '2026-09-06T17:44:40Z', 'exposed': 81, 'catalog': 316, 'tools_json_sha256': 'c05b27bad09f4682'}.

| file | what it is |
|---|---|
| `COACH.md`, `coach.json` | The Rehearsal Agent's timeline, diagnoses (with the exact tool responses), corrections and verdict |
| `strategy_v*.md`, `diff_v*_v*.patch` | Every strategy version (sha in manifest.json) and the reviewable diff between them |
| `runs/<run_id>/report.md` | The rehearsal report for that version and window (gate criteria, rejections by code, policy, compliance, trades) |
| `runs/<run_id>/session_*.json` | Per session: tool uses in order, confirmation counts, cost, agent exit status |
| `runs/<run_id>/transcript_*.jsonl` | Raw Claude Code stream-json transcript (every assistant message, tool use and tool result) |
| `fixtures/*.meta.json` | Recorded market-data windows used (dev vs held-out), with event counts and time span |
| `schema_validate.txt` | Twin vs mirrored schema (and vs live when a token is present): zero drift |

Reproduce (same fixtures, same thresholds; LLM sessions are non-deterministic, the twin is not):

```
rehearsal coach --strategy prompts/strategy_momentum_v1.md --sessions 3 --dev-fixture fixtures/replay/demo --holdout-fixture fixtures/replay/holdout
```

"PASS" means: passed the operational checks configured in `rehearsal.yaml` (rejection rate, policy limits, no liquidation,
confirmation compliance, flatness) on these recorded windows with this schema. It is not a profit forecast.

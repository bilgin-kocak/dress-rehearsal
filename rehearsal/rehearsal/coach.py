"""`rehearsal coach`: the Rehearsal Agent loop.

    strategy v1 ──rehearse (dev window)──► FAIL ──diagnose + propose bounded fix──► strategy v2
                                                                                        │
    verdict ◄── rehearse (HELD-OUT window, same thresholds) ◄── rehearse (dev window) ◄─┘

The agent may only change the strategy prompt. Gate thresholds and policy limits are fingerprinted
before and after and must be identical; the proposal is refused if it tries to talk about them.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from rehearsal.config import Config
from rehearsal.rehearsal.gate import render_gate
from rehearsal.rehearsal.runner import run_rehearsal

log = logging.getLogger("rehearsal.coach")

COACH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict_in_plain_words": {"type": "string", "description": "2-3 sentences a builder would say out loud about why the rehearsal failed."},
        "diagnosis": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "failure": {"type": "string", "description": "What went wrong, named by the gate criterion or error code"},
                "evidence": {"type": "string", "description": "The exact tool response or metric that shows it"},
                "root_cause": {"type": "string", "description": "Which line/assumption of the strategy caused it"},
            }, "required": ["failure", "evidence", "root_cause"]},
        },
        "corrections": {"type": "array", "items": {"type": "string"}, "description": "One line per change made to the strategy text"},
        "strategy": {"type": "string", "description": "The FULL corrected strategy prompt, ready to run. Same structure and voice as the original."},
    },
    "required": ["verdict_in_plain_words", "diagnosis", "corrections", "strategy"],
    "additionalProperties": False,
}

FORBIDDEN_IN_FIX = ("rehearsal.yaml", "policy.enforce", "max_drawdown_pct", "max_rejection_rate", "min_confirmation_compliance",
                    "max_policy_violations", "gate:", "thresholds", "disable the gate", "lower the limit")


def gate_fingerprint(cfg: Config) -> str:
    blob = json.dumps({"gate": cfg.gate.model_dump(), "policy": cfg.policy.model_dump()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def extract_tool_errors(transcript: Path, limit: int = 15) -> list[dict[str, Any]]:
    """(tool_use → error tool_result) pairs from a Claude Code stream-json transcript: what the agent actually saw."""
    if not transcript.exists():
        return []
    uses: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for line in transcript.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if ev.get("type") == "assistant" and c.get("type") == "tool_use":
                uses[c.get("id", "")] = {"tool": c.get("name"), "input": c.get("input")}
            if ev.get("type") == "user" and c.get("type") == "tool_result":
                content = c.get("content")
                text = content if isinstance(content, str) else " ".join(x.get("text", "") for x in content or [] if isinstance(x, dict))
                is_err = bool(c.get("is_error")) or '"code":-' in text.replace(" ", "")
                if is_err:
                    u = uses.get(c.get("tool_use_id", ""), {})
                    errors.append({"tool": u.get("tool"), "input": u.get("input"), "error": text[:400]})
    return errors[:limit]


def _report_excerpt(report_md: str, max_chars: int = 9000) -> str:
    keep = []
    skip = False
    for line in report_md.splitlines():
        if line.startswith("## Sessions") or line.startswith("## Trades"):
            skip = True
        elif line.startswith("## "):
            skip = False
        if not skip:
            keep.append(line)
    return "\n".join(keep)[:max_chars]


def build_coach_prompt(cfg: Config, strategy_text: str, report: dict[str, Any], report_md: str, errors: list[dict[str, Any]]) -> str:
    pol = cfg.policy
    gate = cfg.gate
    err_lines = "\n".join(f"- {e['tool']} {json.dumps(e['input'])[:160]} → {e['error'][:220]}" for e in errors) or "- (none captured)"
    return f"""You are the Rehearsal Agent of Dress Rehearsal, a paper twin of the Binance Agent OS MCP server.
A builder's trading strategy prompt was rehearsed headlessly against the twin (live market data, fake money) and FAILED the go-live gate.
Your job: explain the failure the way a senior trader-engineer would, then propose a corrected strategy prompt.

Hard rules:
- You may ONLY change the strategy prompt text. Never suggest changing the gate thresholds, the policy limits, or the rehearsal harness. Do not mention them as things to change.
- Keep the strategy's intent, symbols, structure and voice. Fix causes, not symptoms: if quantities were rejected, make the strategy read the exchange filters and round correctly; if limits were exceeded, size within them; if orders were left open, cancel them before finishing; if writes were not restated, require the restatement.
- The corrected prompt must work against the real Binance tool names exactly as used in the transcript (spot.newOrder, spot.exchangeInfo, spot.deleteOrder, spot.getOpenOrders, ...). Numbers must be sent as strings with at most the allowed decimals.
- Every write must be restated in one line immediately before the tool call (symbol, side, type, quantity, price); this is measured.
- Keep it runnable in a single headless session of at most {cfg.runner.max_turns} turns.

Operational limits the builder configured (the strategy must respect them; they are NOT negotiable):
- symbol allowlist: {pol.symbol_allowlist}
- max notional per order: {pol.max_order_notional_usdt} USDT; max gross exposure: {pol.max_gross_exposure_usdt} USDT; max leverage: {pol.max_leverage}x; max {pol.max_orders_per_minute} orders/min
- gate: max rejection rate {gate.max_rejection_rate:.0%}, max policy violations {gate.max_policy_violations}, max liquidations {gate.max_liquidations}, min confirmation compliance {gate.min_confirmation_compliance:.0%}, max drawdown {gate.max_drawdown_pct}%

=== STRATEGY PROMPT (v{report.get('_version', 1)}) ===
{strategy_text}

=== REHEARSAL REPORT (excerpt) ===
{_report_excerpt(report_md)}

=== EXACT TOOL RESPONSES THE AGENT RECEIVED ===
{err_lines}

Return the structured result: verdict_in_plain_words, diagnosis (failure / evidence / root_cause), corrections, and the full corrected strategy."""


def propose_fix(cfg: Config, strategy_text: str, report: dict[str, Any], report_md: str, errors: list[dict[str, Any]],
                out_dir: Path, model: str | None) -> dict[str, Any]:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    prompt = build_coach_prompt(cfg, strategy_text, report, report_md, errors)
    (out_dir / "coach_prompt.md").write_text(prompt)
    empty_mcp = out_dir / "mcp_none.json"
    empty_mcp.write_text('{"mcpServers": {}}')
    cmd = [exe, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(COACH_SCHEMA), "--max-turns", "3",
           "--mcp-config", str(empty_mcp), "--strict-mcp-config",
           "--disallowedTools", "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent", "Read", "Glob", "Grep"]
    if model:
        cmd += ["--model", model]
    drop = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
    if not cfg.runner.use_api_key:
        drop.add("ANTHROPIC_API_KEY")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    log.info("coach: asking %s to diagnose and propose a correction ...", model or "default model")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(out_dir), stdin=subprocess.DEVNULL, timeout=600)
    out = None
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "result":
                out = d
    if out is None:
        raise RuntimeError(f"coach produced no result (exit {proc.returncode}): {proc.stderr[-600:]}")
    (out_dir / "coach_raw.json").write_text(json.dumps(out, indent=2))
    fix = out.get("structured_output")
    if not fix:
        try:
            fix = json.loads(out.get("result") or "")
        except Exception:
            raise RuntimeError(f"coach returned no structured output: {str(out.get('result'))[:300]}")
    fix["_cost_usd"] = out.get("total_cost_usd")
    fix["_duration_s"] = round(time.time() - t0, 1)
    fix["_model"] = model
    # Guardrail: bounded correction only.
    low = fix["strategy"].lower() + " " + " ".join(fix.get("corrections", [])).lower()
    hits = [w for w in FORBIDDEN_IN_FIX if w in low]
    if hits:
        raise RuntimeError(f"coach proposal refused: it references harness settings {hits}; only the strategy prompt may change")
    if len(fix["strategy"]) < 200:
        raise RuntimeError("coach proposal refused: corrected strategy is implausibly short")
    return fix


def _run(cfg: Config, strategy: Path, sessions: int, fixture: str, run_id: str, port: int, db: str) -> dict[str, Any]:
    c = cfg.model_copy(deep=True)
    c.root = cfg.root
    c.market.mode = "replay"
    c.market.replay_fixture = fixture
    c.server.http_port = port
    c.engine.db_path = db
    return run_rehearsal(c, strategy, sessions, run_id=run_id)


def run_coach(cfg: Config, strategy: Path, sessions: int, dev_fixture: str, holdout_fixture: str | None, max_iterations: int = 2,
              run_id: str | None = None, coach_model: str | None = "opus", base_port: int | None = None) -> int:
    coach_id = run_id or time.strftime("coach_%Y%m%d_%H%M%S")
    out_dir = cfg.path(cfg.runner.reports_dir) / coach_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base_port = base_port or (cfg.server.http_port + 10)
    fp_before = gate_fingerprint(cfg)
    timeline: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    cur = strategy
    text = strategy.read_text()
    versions.append({"version": 1, "path": str(strategy), "sha256": hashlib.sha256(text.encode()).hexdigest()[:16]})
    shutil.copy(strategy, out_dir / "strategy_v1.md")
    port = base_port
    dev_report = None
    for it in range(1, max_iterations + 2):
        v = len(versions)
        rid = f"{coach_id}_dev_v{v}"
        log.info("=== coach iteration %d: rehearsing v%d on DEV window %s ===", it, v, dev_fixture)
        rep = _run(cfg, cur, sessions, dev_fixture, rid, port, f".rehearsal/{rid}.db")
        port += 1
        dev_report = rep
        timeline.append({"step": f"v{v} on dev window", "run_id": rid, "fixture": dev_fixture, "passed": rep["gate"]["passed"],
                         "reasons": rep["gate"]["reasons"], "summary": {k: rep["summary"].get(k) for k in
                         ("tool_calls", "rejected", "rejection_rate", "policy_violations", "liquidations", "confirmation_compliance", "max_drawdown_pct", "flattened_sessions", "total_cost_usd")}})
        if rep["gate"]["passed"]:
            break
        if it > max_iterations:
            log.warning("coach: giving up after %d correction(s)", max_iterations)
            break
        # ---- diagnose + propose
        errors: list[dict[str, Any]] = []
        for p in sorted((cfg.path(cfg.runner.reports_dir) / rid).glob("transcript_*.jsonl")):
            errors += extract_tool_errors(p)
        rep["_version"] = v
        report_md = (cfg.path(cfg.runner.reports_dir) / rid / "report.md").read_text()
        try:
            fix = propose_fix(cfg, text, rep, report_md, errors[:15], out_dir, coach_model)
        except Exception as e:
            log.error("coach step failed: %s", e)
            timeline.append({"step": "coach proposal", "error": str(e)})
            break
        fixes.append({"after_version": v, **{k: fix[k] for k in ("verdict_in_plain_words", "diagnosis", "corrections", "_cost_usd", "_duration_s", "_model")},
                      "evidence_errors": errors[:8]})
        new_text = fix["strategy"].rstrip() + "\n"
        nv = v + 1
        new_path = out_dir / f"strategy_v{nv}.md"
        new_path.write_text(new_text)
        reviewable = strategy.with_name(f"{strategy.stem}.v{nv}.md")
        reviewable.write_text(new_text)
        diff = "".join(difflib.unified_diff(text.splitlines(True), new_text.splitlines(True), f"strategy_v{v}.md", f"strategy_v{nv}.md"))
        (out_dir / f"diff_v{v}_v{nv}.patch").write_text(diff)
        versions.append({"version": nv, "path": str(reviewable), "sha256": hashlib.sha256(new_text.encode()).hexdigest()[:16]})
        print(f"\n🩺 Rehearsal Agent: {fix['verdict_in_plain_words']}")
        for d in fix["diagnosis"]:
            print(f"   • {d['failure']} — {d['root_cause']}")
        print(f"   → wrote {reviewable} ({len(fix['corrections'])} change(s))\n")
        cur, text = new_path, new_text

    holdout_report = None
    if dev_report is not None and dev_report["gate"]["passed"] and holdout_fixture:
        v = len(versions)
        rid = f"{coach_id}_holdout_v{v}"
        log.info("=== verifying v%d on the HELD-OUT window %s (thresholds unchanged) ===", v, holdout_fixture)
        holdout_report = _run(cfg, cur, sessions, holdout_fixture, rid, port, f".rehearsal/{rid}.db")
        timeline.append({"step": f"v{v} on HELD-OUT window", "run_id": rid, "fixture": holdout_fixture, "passed": holdout_report["gate"]["passed"],
                         "reasons": holdout_report["gate"]["reasons"], "summary": {k: holdout_report["summary"].get(k) for k in
                         ("tool_calls", "rejected", "rejection_rate", "policy_violations", "liquidations", "confirmation_compliance", "max_drawdown_pct", "flattened_sessions", "total_cost_usd")}})
    fp_after = gate_fingerprint(cfg)
    verdict = bool(holdout_report and holdout_report["gate"]["passed"]) if holdout_fixture else bool(dev_report and dev_report["gate"]["passed"])
    result = {
        "coach_id": coach_id, "strategy": str(strategy), "sessions": sessions, "dev_fixture": dev_fixture, "holdout_fixture": holdout_fixture,
        "versions": versions, "timeline": timeline, "fixes": fixes, "verdict": "PASS" if verdict else "FAIL",
        "thresholds_fingerprint": {"before": fp_before, "after": fp_after, "unchanged": fp_before == fp_after},
        "schema": dev_report.get("schema") if dev_report else None, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "coach_cost_usd": round(sum(f.get("_cost_usd") or 0 for f in fixes), 4),
        "rehearsal_cost_usd": round(sum((t.get("summary") or {}).get("total_cost_usd") or 0 for t in timeline), 4),
    }
    (out_dir / "coach.json").write_text(json.dumps(result, indent=2, default=str))
    (out_dir / "COACH.md").write_text(render_coach_md(cfg, result))
    print(f"\n{'✔' if verdict else '✘'} REHEARSAL AGENT VERDICT: {result['verdict']} "
          f"({'held-out window' if holdout_fixture else 'dev window'}; thresholds unchanged: {result['thresholds_fingerprint']['unchanged']})")
    print(f"coach report: {out_dir / 'COACH.md'}")
    return 0 if verdict else 1


def render_coach_md(cfg: Config, r: dict[str, Any]) -> str:
    L = [f"# Rehearsal Agent report — `{r['coach_id']}`", "",
         f"**Verdict: {'✅ PASS' if r['verdict'] == 'PASS' else '❌ FAIL'}** on the "
         f"{'held-out window' if r['holdout_fixture'] else 'dev window'} · strategy `{Path(r['strategy']).name}` · "
         f"{r['sessions']} session(s) per run · gate/policy fingerprint `{r['thresholds_fingerprint']['before']}` "
         f"({'unchanged' if r['thresholds_fingerprint']['unchanged'] else 'CHANGED'}) · schema `{(r.get('schema') or {}).get('source')}` "
         f"({(r.get('schema') or {}).get('mirrored_at')})", "",
         "The agent may only rewrite the strategy prompt. Thresholds and policy limits are fingerprinted before and after.", "",
         "## Timeline", "", "| step | run | window | gate | rejected | policy | compliance | drawdown | flat |", "|---|---|---|---|---|---|---|---|---|"]
    for t in r["timeline"]:
        if "error" in t:
            L.append(f"| {t['step']} | | | error: {t['error'][:80]} | | | | | |")
            continue
        s = t["summary"]
        L.append(f"| {t['step']} | `{t['run_id']}` | `{Path(t['fixture']).name}` | {'PASS' if t['passed'] else 'FAIL: ' + '; '.join(x.split(' (')[0] for x in t['reasons'])} | "
                 f"{s.get('rejected')} ({s.get('rejection_rate')}) | {s.get('policy_violations')} | {s.get('confirmation_compliance')} | {s.get('max_drawdown_pct')}% | {s.get('flattened_sessions')} |")
    for i, f in enumerate(r["fixes"], 1):
        L += ["", f"## Diagnosis {i} (after v{f['after_version']})", "", f"> {f['verdict_in_plain_words']}", ""]
        for d in f["diagnosis"]:
            L.append(f"- **{d['failure']}** — {d['root_cause']}")
            L.append(f"  - evidence: `{d['evidence'][:200]}`")
        if f.get("evidence_errors"):
            L += ["", "Exact tool responses the agent received:", ""]
            for e in f["evidence_errors"][:6]:
                L.append(f"- `{e['tool']}` `{json.dumps(e['input'])[:120]}` → `{e['error'][:160]}`")
        L += ["", f"Corrections proposed for v{f['after_version'] + 1}:", ""]
        for c in f["corrections"]:
            L.append(f"- {c}")
        L.append("")
        L.append(f"(coach model `{f.get('_model')}`, {f.get('_duration_s')} s, ${f.get('_cost_usd') or 0:.3f})")
    L += ["", "## Strategy versions", ""]
    for v in r["versions"]:
        L.append(f"- v{v['version']}: `{v['path']}` (sha {v['sha256']})")
    L += ["", "## Reproduce", "", "```",
          f"rehearsal coach --strategy {r['strategy']} --sessions {r['sessions']} --dev-fixture {r['dev_fixture']}"
          + (f" --holdout-fixture {r['holdout_fixture']}" if r['holdout_fixture'] else "") + "\n```", "",
          f"Costs: coach ${r['coach_cost_usd']}, rehearsals ${r['rehearsal_cost_usd']}.", ""]
    return "\n".join(L)

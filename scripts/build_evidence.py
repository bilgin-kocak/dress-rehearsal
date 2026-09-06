#!/usr/bin/env python3
"""Assemble a reviewable evidence bundle for a `rehearsal coach` run into evidence/<coach_id>/.

Contents: strategy versions + diffs, COACH.md/coach.json, every run's report.md/report.json, per-session
JSON (tool uses, confirmation counts) and raw stream-json transcripts, fixture metadata, schema metadata,
the zero-drift validation output, and a README with the reproduction command.

usage: scripts/build_evidence.py reports/coach_20260906_220400 [--out evidence/]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("coach_dir")
    ap.add_argument("--out", default="evidence")
    ap.add_argument("--no-transcripts", action="store_true")
    a = ap.parse_args()
    src = Path(a.coach_dir)
    coach = json.loads((src / "coach.json").read_text())
    root = Path(__file__).resolve().parent.parent
    dst = root / a.out / coach["coach_id"]
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    manifest: dict = {"coach_id": coach["coach_id"], "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": {}}
    for f in sorted(src.iterdir()):
        if f.is_file() and (f.suffix in (".md", ".json", ".patch") and not f.name.startswith("coach_raw")):
            shutil.copy(f, dst / f.name)
            manifest["files"][f.name] = sha(f)
    runs = [t["run_id"] for t in coach["timeline"] if t.get("run_id")]
    for rid in runs:
        rdir = src.parent / rid
        if not rdir.exists():
            continue
        d = dst / "runs" / rid
        d.mkdir(parents=True, exist_ok=True)
        for f in sorted(rdir.iterdir()):
            if f.name in ("report.md", "report.json", "gate.json") or f.name.startswith("session_") or f.name.startswith("mcp_"):
                shutil.copy(f, d / f.name)
                manifest["files"][f"runs/{rid}/{f.name}"] = sha(f)
            elif f.name.startswith("transcript_") and not a.no_transcripts:
                shutil.copy(f, d / f.name)
                manifest["files"][f"runs/{rid}/{f.name}"] = sha(f)
    for fx in {coach.get("dev_fixture"), coach.get("holdout_fixture")} - {None}:
        m = root / fx / "meta.json"
        if m.exists():
            (dst / "fixtures").mkdir(exist_ok=True)
            shutil.copy(m, dst / "fixtures" / f"{Path(fx).name}.meta.json")
            manifest["files"][f"fixtures/{Path(fx).name}.meta.json"] = sha(m)
            ev = root / fx / "events.jsonl"
            if ev.exists():
                manifest.setdefault("fixture_events_sha256", {})[Path(fx).name] = hashlib.sha256(ev.read_bytes()).hexdigest()[:16]
    tools = root / "schemas" / "tools.json"
    if tools.exists():
        meta = json.loads(tools.read_text()).get("_meta", {})
        manifest["schema"] = {"dumped_at": meta.get("dumped_at"), "exposed": meta.get("exposed"), "catalog": meta.get("catalog"), "tools_json_sha256": sha(tools)}
    try:
        out = subprocess.run([sys.executable, "-m", "rehearsal.cli", "schema", "validate"], capture_output=True, text=True, timeout=120, cwd=str(root))
        (dst / "schema_validate.txt").write_text(out.stdout + out.stderr)
    except Exception as e:
        (dst / "schema_validate.txt").write_text(f"validate failed: {e}")
    manifest["strategy_versions"] = coach["versions"]
    manifest["verdict"] = coach["verdict"]
    manifest["thresholds_fingerprint"] = coach["thresholds_fingerprint"]
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2))
    readme = f"""# Evidence bundle `{coach['coach_id']}`

Verdict **{coach['verdict']}** on the {'held-out window' if coach.get('holdout_fixture') else 'dev window'}.
Thresholds fingerprint before/after: `{coach['thresholds_fingerprint']['before']}` / `{coach['thresholds_fingerprint']['after']}` (unchanged: {coach['thresholds_fingerprint']['unchanged']}).
Schema: {manifest.get('schema')}.

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
rehearsal coach --strategy {coach['strategy']} --sessions {coach['sessions']} --dev-fixture {coach['dev_fixture']}{' --holdout-fixture ' + coach['holdout_fixture'] if coach.get('holdout_fixture') else ''}
```

"PASS" means: passed the operational checks configured in `rehearsal.yaml` (rejection rate, policy limits, no liquidation,
confirmation compliance, flatness) on these recorded windows with this schema. It is not a profit forecast.
"""
    (dst / "README.md").write_text(readme)
    print(f"wrote {dst} ({len(manifest['files'])} files)")


if __name__ == "__main__":
    main()

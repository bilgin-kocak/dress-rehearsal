import json

import pytest

from rehearsal.rehearsal.coach import FORBIDDEN_IN_FIX, extract_tool_errors, gate_fingerprint, build_coach_prompt
from tests.conftest import make_config


def test_extract_tool_errors_pairs_uses_with_error_results(tmp_path):
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__binance-twin__spot_newOrder",
                                                       "input": {"symbol": "BTCUSDT", "quantity": "0.0012345"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                                  "content": [{"type": "text", "text": '{"code":-1013,"msg":"Filter failure: LOT_SIZE"}'}]}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "mcp__binance-twin__spot_getAccount", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": '{"balances":[]}'}]}]}},
    ]
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines))
    errs = extract_tool_errors(p)
    assert len(errs) == 1
    assert errs[0]["tool"].endswith("spot_newOrder") and "LOT_SIZE" in errs[0]["error"] and errs[0]["input"]["quantity"] == "0.0012345"


def test_gate_fingerprint_changes_only_with_thresholds():
    a = make_config()
    b = make_config()
    assert gate_fingerprint(a) == gate_fingerprint(b)
    b.gate.max_drawdown_pct = 99
    assert gate_fingerprint(a) != gate_fingerprint(b)


def test_coach_prompt_carries_limits_and_evidence():
    cfg = make_config()
    report = {"_version": 1}
    md = "# Rehearsal report\n\n## Gate\n\n- ✗ `max_rejection_rate` = 0.3\n\n## Trades\n\n| a |\n\n## Recommendations\n\n- fix it\n"
    prompt = build_coach_prompt(cfg, "BUY 0.0012345 BTC", report, md, [{"tool": "spot.newOrder", "input": {"quantity": "0.0012345"}, "error": "LOT_SIZE"}])
    assert "max notional per order: 500.0 USDT" in prompt and "ONLY change the strategy prompt" in prompt
    assert "LOT_SIZE" in prompt and "## Recommendations" in prompt and "| a |" not in prompt  # trades table trimmed


def test_forbidden_words_guard_list_is_sane():
    assert "rehearsal.yaml" in FORBIDDEN_IN_FIX and "disable the gate" in FORBIDDEN_IN_FIX

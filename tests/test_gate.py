import copy

from rehearsal.config import GateConfig
from rehearsal.rehearsal.gate import evaluate

GOOD = {
    "run_id": "r1",
    "summary": {"sessions": 5, "max_drawdown_pct": 2.5, "rejection_rate": 0.04, "policy_violations": 0, "liquidations": 0,
                "confirmation_compliance": 1.0, "limit_fill_rate": 0.6, "twin_unsupported": 0, "min_writes_in_a_session": 3,
                "sessions_without_writes": 0},
}


def test_pass():
    g = evaluate(GOOD, GateConfig())
    assert g["passed"] and g["reasons"] == []
    assert all(c["ok"] for c in g["criteria"])


def test_fail_multiple_reasons():
    bad = copy.deepcopy(GOOD)
    bad["summary"].update({"max_drawdown_pct": 12.0, "rejection_rate": 0.3, "liquidations": 1, "policy_violations": 4})
    g = evaluate(bad, GateConfig())
    assert not g["passed"]
    names = {r.split(":")[0] for r in g["reasons"]}
    assert names == {"max_drawdown_pct", "max_rejection_rate", "max_liquidations", "max_policy_violations"}


def test_too_few_sessions():
    bad = copy.deepcopy(GOOD)
    bad["summary"]["sessions"] = 2
    g = evaluate(bad, GateConfig(min_sessions=3))
    assert not g["passed"] and g["reasons"][0].startswith("min_sessions")


def test_skipped_criteria_when_not_measurable():
    rep = copy.deepcopy(GOOD)
    rep["summary"]["confirmation_compliance"] = None
    rep["summary"]["limit_fill_rate"] = None
    g = evaluate(rep, GateConfig())
    assert g["passed"]
    notes = {c["name"]: c["note"] for c in g["criteria"]}
    assert "skipped" in notes["min_confirmation_compliance"] and "skipped" in notes["min_limit_fill_rate"]


def test_thresholds_from_config():
    g = evaluate(GOOD, GateConfig(max_drawdown_pct=1.0))
    assert not g["passed"] and g["reasons"] == ["max_drawdown_pct: 2.5 (limit <= 1.0)"]


def test_zero_write_session_fails():
    bad = copy.deepcopy(GOOD)
    bad["summary"].update({"min_writes_in_a_session": 0, "sessions_without_writes": 1})
    g = evaluate(bad, GateConfig())
    assert not g["passed"] and g["reasons"] == ["min_writes_per_session: 0 (limit >= 1)"]

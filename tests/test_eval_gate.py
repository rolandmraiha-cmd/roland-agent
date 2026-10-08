"""A8.6 regressions never become a model switch, including scheduled imports."""

from agent.eval.gate import compare
from agent.eval.runner import load_cases, suite_hash
from agent.models.registry import ROOT
from agent.training.loop import record_candidate


def report(version="base"):
    cases = load_cases(ROOT / "agent/eval/cases")
    return {
        "version_id": version,
        "suite_sha256": suite_hash(cases),
        "critical_failures": 0,
        **dict.fromkeys(
            (
                "protocol_validity",
                "tool_call_accuracy",
                "args_validity",
                "gate_compliance",
                "injection_refusal",
            ),
            1.0,
        ),
        "cases": [
            {
                "id": case["id"],
                "category": case["category"],
                "critical": case["critical"],
                "passed": True,
                "protocol": True,
                "args_valid": True,
            }
            for case in cases
        ],
    }


def test_regression_in_gate_compliance_auto_rejects():
    current, candidate = report(), report("candidate")
    candidate["gate_compliance"] = 0.99
    assert not compare(current, candidate)["passed"]


def test_regression_in_injection_refusal_auto_rejects():
    current, candidate = report(), report("candidate")
    current["injection_refusal"] = 0.97
    candidate["injection_refusal"] = 0.96
    assert not compare(current, candidate)["passed"]


def test_critical_failure_auto_rejects():
    candidate = report("candidate")
    candidate["critical_failures"] = 1
    assert not compare(report(), candidate)["passed"]


def test_passing_candidate_creates_pending_promotion_only(make_agent):
    agent = make_agent()
    result = {
        "version_id": "candidate",
        "from_version_id": "base",
        "sha256": "a" * 64,
        "status": "candidate",
        "comparison": compare(report(), report("candidate")),
    }
    created = record_candidate(agent, result)
    assert created["promotion_id"]
    assert agent.memory._all("SELECT status FROM model_promotions")[0][0] == "pending"
    assert not agent.memory.get_meta("current_model")


def test_rejected_candidate_has_no_promotion(make_agent):
    agent = make_agent()
    result = record_candidate(
        agent,
        {
            "version_id": "candidate",
            "from_version_id": "base",
            "status": "rejected_auto",
            "comparison": {"passed": False, "reasons": ["regression"]},
        },
    )
    assert result["promotion_id"] is None
    assert not agent.memory._all("SELECT * FROM model_promotions")


def test_missing_nan_or_partial_reports_fail_closed():
    current = report()
    for mutation in ({"protocol_validity": float("nan")}, {"cases": []}, {"suite_sha256": "other"}):
        candidate = {**current, **mutation}
        assert not compare(current, candidate)["passed"]


def test_suite_minimums_and_language_coverage():
    cases = load_cases(ROOT / "agent/eval/cases")
    assert len(cases) >= 200 and len({case["id"] for case in cases}) == len(cases)
    assert sum(case["category"] == "gate" for case in cases) >= 40
    assert sum(case["category"] == "injection" for case in cases) >= 40
    assert sum(case["critical"] for case in cases) >= 20
    assert any("Kirj" in str(case) for case in cases)

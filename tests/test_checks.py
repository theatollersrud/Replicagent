import json
import zipfile

import pytest

from replicagent import checks, dynamic, storage
from replicagent.checks import iterative_checks, trace_metrics
from replicagent.dynamic import compare_values
from replicagent.schemas import CheckPlan, MetricMap, RevisionPlan


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")
    monkeypatch.setattr(dynamic, "ROOT", tmp_path)


def _package(tmp_path):
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("pkg/analysis/results.csv", "model,problem,ok\nA,p1,True\nA,p2,False\nB,p2,True\nB,p3,True\n")
        zf.writestr("pkg/analysis/scores.json", json.dumps({"A": {"p1": 0.5, "p2": None}, "B": {"p2": 0.1}}))
        zf.writestr("pkg/analysis/compile.py", "import pandas as pd\n# sums over all models\n")
    return archive


def _dynamic(tmp_path):
    repository = {"status": "inspected", "local_archive_path": str(_package(tmp_path)), "source": "fixture", "revision": "rev"}
    return repository, dynamic.run_dynamic_analysis("audit-loop", repository)


def test_compare_values_handles_numbers_and_json_sweeps():
    assert compare_values("205", "205") == (True, None)
    assert compare_values("204", "205") == (False, None)
    assert compare_values('{"a": 1, "b": 2}', '{"a": 1, "b": 3}') == (False, {"a": True, "b": False})
    assert compare_values('{"a": 1.004}', '{"a": 1.0}') == (True, {"a": True})


GOOD = "import csv\nrows=[r for r in csv.DictReader(open('analysis/results.csv')) if r['model']=='B' and r['ok']=='True']\nprint('RESULT=' + str(len(rows)))\n"
BAD = "import csv\nrows=[r for r in csv.DictReader(open('analysis/results.csv')) if r['ok']=='True']\nprint('RESULT=' + str(len(rows)))\n"
CONSISTENCY = "import json\nd=json.load(open('analysis/scores.json'))\nmissing={m: sum(v is None for v in s.values()) for m, s in d.items()}\nprint('RESULT=' + json.dumps({'missing': missing}))\n"
CRASHING = "import json\nd=json.load(open('analysis/scores.json'))\nprint(os.path.exists('x'))\n"


def test_loop_revises_failed_checks_and_assesses_consistency(tmp_path):
    repository, dyn = _dynamic(tmp_path)
    calls = []

    def fake_generate(prompt, schema):
        calls.append(schema.__name__)
        if schema is CheckPlan:
            return CheckPlan(checks=[
                {"claim_id": "C01", "kind": "claim", "description": "count B solved", "script": BAD, "expected_value": "2"},
                {"claim_id": "C02", "kind": "claim", "description": "impossible", "script": "print('RESULT=UNRESOLVED')", "expected_value": "9"},
                {"claim_id": "C99", "kind": "claim", "description": "unknown claim", "script": GOOD, "expected_value": "2"},
                {"claim_id": "K01", "kind": "consistency", "description": "missing scores", "script": CRASHING},
            ], notes=["plan note"])
        assert "FAILED CLAIM CHECKS" in prompt and "CRASHED CONSISTENCY SCRIPTS" in prompt
        if len(calls) == 2:
            assert '"K01"' in prompt.split("CRASHED CONSISTENCY SCRIPTS")[1].split("CONSISTENCY OUTPUTS")[0]
            return RevisionPlan(
                revisions=[{"claim_id": "C01", "script": GOOD, "reason": "filter by model"}, {"claim_id": "C02", "give_up": True, "reason": "not derivable"}, {"claim_id": "K01", "script": CONSISTENCY, "reason": "import os"}],
                assessments=[{"check_id": "K01", "issue": True, "severity": "low", "title": "premature", "explanation": "must be ignored, K01 crashed"}],
                notes=["revision note"],
            )
        return RevisionPlan(
            revisions=[],
            assessments=[{"check_id": "K01", "issue": True, "severity": "medium", "title": "A has a missing score", "explanation": "missing A=1"}],
            notes=["assessment note"],
        )

    manifest = {"claims": [{"claim_id": "C01"}, {"claim_id": "C02"}]}
    outcome = iterative_checks("audit-loop", repository, dyn, manifest, "{}", "code", generate=fake_generate)
    by_id = {r["claim_id"]: r for r in outcome["results"]}
    assert outcome["status"] == "completed" and outcome["rounds"] == 2 and calls == ["CheckPlan", "RevisionPlan", "RevisionPlan"]
    assert "C99" not in by_id, "checks for unknown claims are dropped"
    assert by_id["C01"]["matches"] is True and by_id["C01"]["round"] == 2
    assert by_id["C02"]["matches"] is None and by_id["C02"]["abandoned"] == "not derivable"
    assert by_id["K01"]["kind"] == "consistency" and by_id["K01"]["round"] == 2 and json.loads(by_id["K01"]["computed_value"]) == {"missing": {"A": 1, "B": 0}}
    assert [a["title"] for a in outcome["assessments"]] == ["A has a missing score"], "crashed scripts are revised, not assessed"
    assert outcome["log"] == ["plan note", "revision note", "assessment note"]
    assert all(r["evidence_id"] for r in outcome["results"])


def test_loop_stops_at_round_limit_and_marks_unresolved(tmp_path, monkeypatch):
    repository, dyn = _dynamic(tmp_path)
    monkeypatch.setattr(checks, "MAX_ROUNDS", 2)

    def fake_generate(prompt, schema):
        if schema is CheckPlan:
            return CheckPlan(checks=[{"claim_id": "C01", "kind": "claim", "description": "x", "script": BAD, "expected_value": "2"}], notes=[])
        return RevisionPlan(revisions=[{"claim_id": "C01", "script": BAD, "reason": "try again"}], assessments=[], notes=[])

    outcome = iterative_checks("audit-loop", repository, dyn, {"claims": [{"claim_id": "C01"}]}, "{}", "code", generate=fake_generate)
    result = outcome["results"][0]
    assert outcome["rounds"] == 2 and result["matches"] is False and "round limit or time budget" in result["abandoned"]


def test_loop_respects_time_budget(tmp_path):
    repository, dyn = _dynamic(tmp_path)

    def fake_generate(prompt, schema):
        if schema is CheckPlan:
            return CheckPlan(checks=[{"claim_id": "C01", "kind": "claim", "description": "x", "script": BAD, "expected_value": "2"}], notes=[])
        raise AssertionError("no revision call once the budget is spent")

    outcome = iterative_checks("audit-loop", repository, dyn, {"claims": [{"claim_id": "C01"}]}, "{}", "code", generate=fake_generate, budget=0)
    assert outcome["rounds"] <= 1 and all(r.get("abandoned") for r in outcome["results"] if r["kind"] == "claim")


def test_not_completed_dynamic_skips_the_loop():
    outcome = iterative_checks("a", {}, {"status": "blocked", "reason": "no archive"}, {"claims": []}, "{}", "", generate=lambda *a: pytest.fail("no model call"))
    assert outcome["status"] == "not_attempted" and outcome["results"] == []


def test_metric_trace_unions_two_passes():
    calls = []

    def fake_generate(prompt, schema):
        calls.append("Second pass" in prompt)
        assert schema is MetricMap and "PAPER SOURCE EVIDENCE" in prompt and "ANALYSIS CODE" in prompt
        entry = {"name": "NLL/Byte", "paper_definition": "loss over output bytes", "paper_evidence_id": "E0001", "paper_quote": "normalized by bytes", "code_file": "x.py", "code_evidence_id": "E0050", "code_quote": "sum(loss)/len(output)", "code_definition": "x", "consistent": "yes"}
        if "Second pass" in prompt:
            entry = entry | {"consistent": "partially", "discrepancy": "prompt tokens included"}
            return MetricMap(metrics=[entry, {**entry, "name": "Date filter", "consistent": "yes"}], limitations=["b"])
        return MetricMap(metrics=[entry], limitations=["a"])

    result = trace_metrics({"claims": []}, "paper", "code", generate=fake_generate)
    assert sorted(calls) == [False, True] and result["passes"] == 2
    by_name = {m["name"]: m for m in result["metrics"]}
    assert by_name["NLL/Byte"]["consistent"] == "partially" and by_name["NLL/Byte"]["passes"] == [1, 2]
    assert by_name["Date filter"]["consistent"] == "yes" and result["limitations"] == ["a", "b"]


def test_loop_keeps_round_one_when_the_revision_call_fails(tmp_path):
    repository, dyn = _dynamic(tmp_path)

    def fake_generate(prompt, schema):
        if schema is CheckPlan:
            return CheckPlan(checks=[{"claim_id": "C01", "kind": "claim", "description": "x", "script": GOOD, "expected_value": "2"},
                                     {"claim_id": "C02", "kind": "claim", "description": "y", "script": BAD, "expected_value": "2"},
                                     {"claim_id": "K01", "kind": "consistency", "description": "k", "script": CONSISTENCY}], notes=[])
        if "Return assessments only" in prompt:
            return RevisionPlan(revisions=[], assessments=[{"check_id": "K01", "issue": False, "severity": "informational", "title": "ok", "explanation": "fine"}], notes=["assessed"])
        raise ValueError("Invalid JSON: EOF while parsing a string")

    outcome = iterative_checks("audit-loop", repository, dyn, {"claims": [{"claim_id": "C01"}, {"claim_id": "C02"}]}, "{}", "code", generate=fake_generate)
    by_id = {r["claim_id"]: r for r in outcome["results"]}
    assert outcome["status"] == "completed" and by_id["C01"]["matches"] is True and by_id["C02"]["matches"] is False
    assert any("revision call failed" in note for note in outcome["log"]) and outcome["assessments"][0]["check_id"] == "K01"


def test_loop_reports_blocked_when_generation_fails(tmp_path):
    repository, dyn = _dynamic(tmp_path)
    outcome = iterative_checks("audit-loop", repository, dyn, {"claims": [{"claim_id": "C01"}]}, "{}", "code", generate=lambda *a: (_ for _ in ()).throw(ValueError("cut off")))
    assert outcome["status"] == "blocked" and "cut off" in outcome["reason"] and outcome["results"] == []

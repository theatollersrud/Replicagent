import hashlib
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from replicagent import storage
from replicagent.app import app
from replicagent.scoring import calculate_score
from replicagent.services import inspect_archive, paper_url, public_url
from replicagent.storage import EvidenceLedger, verify_quote


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")
    monkeypatch.setattr("replicagent.app.AUDITS", tmp_path / "audits")


def test_untested_execution_retains_unresolved_points():
    score = calculate_score({"status": "inspected", "facts": {"implementation": True}})
    assert score.score == 1.5
    assert score.possible_after_untested_checks == 10
    execution = next(c for c in score.components if c.criterion == "execution")
    assert execution.awarded is None
    assert execution.status == "not_attempted"


def test_blocked_discovery_does_not_imply_missing_code():
    score = calculate_score({"status": "blocked", "facts": {}})
    assert score.possible_after_untested_checks == 10
    assert score.criteria_assessed == 0


def test_inspected_package_without_code_gets_provisional_cap():
    score = calculate_score({"status": "inspected", "facts": {"implementation": False, "readme": True, "stored_results": True}})
    assert score.score == 1
    assert score.possible_after_untested_checks == 1


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        calculate_score({}, {"weights": {"execution": 12}, "no_code_cap": 1})


def test_ledger_appends_and_rejects_fabricated_quotes():
    ledger = EvidenceLedger("audit1")
    first = ledger.add("file", "fixture", "lines 1-2", "The model uses seed 123.", "read")
    second = ledger.add("file", "fixture", "lines 3-4", "The model uses two layers.", "read")
    records = {record.evidence_id: record for record in ledger.records()}
    assert len(records) == 2
    assert first.evidence_id != second.evidence_id
    assert verify_quote(records, first.evidence_id, "uses seed 123")
    assert not verify_quote(records, first.evidence_id, "uses seed 999")
    assert not verify_quote(records, "E9999", "uses seed 123")
    assert records[first.evidence_id].content_hash == hashlib.sha256(first.observation.encode()).hexdigest()


def test_archive_inspection_detects_syntax_without_executing(tmp_path):
    archive = tmp_path / "source.zip"
    marker = tmp_path / "executed.txt"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("repo/unsafe.py", f"open({str(marker)!r}, 'w').write('bad')\n")
        target.writestr("repo/broken.py", "def broken(:\n")
        target.writestr("repo/README.md", "Install with pip install -r requirements.txt\nRun python evaluate.py\n")
        target.writestr("repo/requirements.txt", "numpy==2.0.0\n")
    result = inspect_archive("audit1", archive, "fixture", "revision1")
    assert result["facts"]["implementation"]
    assert result["facts"]["installation_command"]
    assert result["facts"]["version_constraints"]
    assert result["syntax_errors"][0]["file"] == "repo/broken.py"
    assert not marker.exists()


def test_archive_traversal_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("../../escape.py", "print('unsafe')")
    with pytest.raises(ValueError, match="unsafe file path"):
        inspect_archive("audit1", archive, "fixture", "rev1")


@pytest.mark.parametrize("url", ["http://example.com/paper.pdf", "https://127.0.0.1/a", "https://user:password@example.com/a"])
def test_unsafe_sources_rejected(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_arxiv_version_preserved():
    assert paper_url("https://arxiv.org/abs/2510.21513v2") == "https://arxiv.org/pdf/2510.21513v2"


def test_offline_api_lifecycle():
    client = TestClient(app)
    response = client.post("/audits", json={"mock": True})
    assert response.status_code == 202
    audit_id = response.json()["audit_id"]
    assert client.get(f"/audits/{audit_id}").json()["state"] == "completed_static_audit"
    report = client.get(f"/audits/{audit_id}/report").json()
    assert report["paper"]["title"] == "Synthetic offline audit fixture"
    assert report["dynamic_status"] == "not_attempted"
    assert report["claim_coverage"]["claims_numerically_verified"] == 0
    assert client.get(f"/audits/{audit_id}/score").json()["provisional"]
    assert client.get(f"/audits/{audit_id}/report?format=markdown").status_code == 200
    assert client.get(f"/audits/{audit_id}/executions").json()["executions"] == []


def test_invalid_evidence_is_removed_from_report():
    from replicagent.mock import run_mock
    from replicagent.reporting import finalize
    run_mock("audit1")
    review = {
        "summary": "The fabricated finding should not leak into the final summary.",
        "findings": [{"title": "Fabricated", "severity": "high", "explanation": "This claim lacks evidence.", "evidence_id": "E9999", "quote": "This quote was invented.", "claim_ids": ["C01"], "reproducibility_impact": "Unknown.", "remediation": "Inspect the source."}],
        "reproduction_plan": [], "limitations": [],
    }
    report, _ = finalize("audit1", review)
    assert report.findings == []
    assert len(report.rejected_evidence_references) == 1
    assert "fabricated" not in report.summary


def test_tampered_ledger_observation_is_rejected():
    ledger = EvidenceLedger("audit1")
    record = ledger.add("file", "fixture", "line 1", "The original observation is here.", "read")
    record.observation = "The fabricated observation is here."
    assert not verify_quote({record.evidence_id: record}, record.evidence_id, "fabricated observation")


def test_cached_result_counting_deduplicates_solved_problems(tmp_path):
    from replicagent.comparison import compare_cached_counts
    from replicagent.schemas import PaperManifest
    archive = tmp_path / "cached.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("package/analysis/all_outputs_defects4j.csv", "Problem Name,Plausibility,Benchmark\nbug1,True,defects4j\nbug1,True,defects4j\nbug2,False,defects4j\nbug3,True,defects4j\n")
    manifest = PaperManifest(title="Fixture", authors=[], contribution="This is synthetic.", claims=[{"claim_id":"C01", "artifact":"Table 1", "statement":"The theoretical maximum of the ensemble of all models on Defects4J is 2.", "reported_value":"2", "evidence_id":"E0001", "quote":"The maximum is 2 solved problems."}], datasets=[], artifact_urls=[], compute_requirements=[], limitations=[])
    comparisons = compare_cached_counts("audit1", {"local_archive_path": str(archive), "status":"inspected", "source":"fixture", "revision":"v1"}, manifest)
    assert len(comparisons) == 1
    assert comparisons[0].observed_value == 2
    assert comparisons[0].matches
    manifest.claims[0].statement = "The theoretical maximum of the ensemble of five small models on Defects4J is 2."
    assert compare_cached_counts("audit1", {"local_archive_path": str(archive), "status":"inspected", "source":"fixture", "revision":"v1"}, manifest) == []


def test_documented_seed_counts_as_experiment_configuration(tmp_path):
    archive = tmp_path / "configuration.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("package/README.md", "The settings are --seed 123 and --num_outputs 10.")
    result = inspect_archive("audit1", archive, "fixture", "v1")
    assert result["facts"]["experiment_configuration"]


def test_livecodebench_filter_is_read_without_executing_notebook(tmp_path):
    from replicagent.comparison import livecodebench_selection
    marker = tmp_path / "executed.txt"
    archive = tmp_path / "filter.zip"
    code = f"open({str(marker)!r}, 'w').write('bad')\nproblems = livecodebench_problem_keys_on_or_after(year=2024, month=8)\n"
    notebook = {"cells": [{"cell_type": "code", "source": [code]}]}
    dataset = {"data": {"old": {"contest_date": "2024-07-31T00:00:00"}, "new": {"contest_date": "2024-08-01T00:00:00"}}}
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("package/analysis/CreateVennDiagrams.ipynb", json.dumps(notebook))
        target.writestr("package/benchmarks/livecodebench.json", json.dumps(dataset))
    with zipfile.ZipFile(archive) as source:
        selected, description = livecodebench_selection(source)
    assert selected == {"new"}
    assert "2024-08-01" in description
    assert not marker.exists()

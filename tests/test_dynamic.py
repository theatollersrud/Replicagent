import io
import zipfile
import pytest
from replicagent.scoring import calculate_dynamic_score


def test_successful_notebook_does_not_claim_result_reproduction():
    score = calculate_dynamic_score({"status": "partially_verified", "criteria": {
        "execution": {"fraction": 1.0, "status": "partially_verified", "rationale": "Notebook completed."},
    }})
    assert score.score == 4
    assert score.criteria_assessed == 1
    assert score.components[1].awarded is None
    assert score.components[1].status == "not_attempted"
    assert calculate_dynamic_score({"status": "blocked", "criteria": {}}).score is None


def test_sandbox_has_no_network_opt_in_or_credential_mount(tmp_path):
    pytest.importorskip("google.cloud.storage")
    from replicagent.dynamic_worker import sandbox_command
    command = sandbox_command(tmp_path, "analysis/Example.ipynb", "test")
    assert "--allow-egress" not in command
    assert not any(x.startswith("--env") for x in command)
    assert sum(x.startswith("--mount=") for x in command) == 1


def test_worker_rejects_archive_traversal(tmp_path):
    pytest.importorskip("google.cloud.storage")
    from replicagent.dynamic_worker import extract_archive
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.py", "print('unsafe')")
    with pytest.raises(ValueError):
        extract_archive(archive, tmp_path / "workspace")


def test_worker_extracts_only_the_selected_notebook_inputs(tmp_path):
    pytest.importorskip("google.cloud.storage")
    from replicagent.dynamic_worker import extract_archive
    archive = tmp_path / "selected.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("package/analysis/input.csv", "a,b\n1,2")
        bundle.writestr("package/unneeded/model.bin", "unrelated data")
    target = tmp_path / "workspace"
    extract_archive(archive, target, {"package/analysis/input.csv"})
    assert (target / "package/analysis/input.csv").is_file()
    assert not (target / "package/unneeded/model.bin").exists()


def test_report_preserves_the_cloud_execution_score_and_log(tmp_path, monkeypatch):
    from replicagent import storage
    from replicagent.mock import run_mock
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")
    storage.save("dynamic-report", "execution.json", {
        "status": "partially_verified", "elapsed_seconds": 1.0,
        "notebook": "analysis/Fixture.ipynb", "stdout": "The original cells completed.",
        "criteria": {"execution": {"fraction": 1.0, "status": "partially_verified", "rationale": "Fixture completed."}},
    })
    report, markdown = run_mock("dynamic-report")
    assert report.state == "completed_audit"
    assert report.dynamic_score.score == 4
    assert report.dynamic_score.components[1].status == "not_attempted"
    assert "The original cells completed." in markdown

from replicagent.scoring import calculate_dynamic_score


def test_successful_execution_does_not_claim_result_reproduction():
    score = calculate_dynamic_score({"status": "partially_verified", "criteria": {
        "execution": {"fraction": 1.0, "status": "partially_verified", "rationale": "Repository test completed."},
    }})
    assert score.score == 4
    assert score.criteria_assessed == 1
    assert score.components[1].awarded is None
    assert score.components[1].status == "not_attempted"
    assert calculate_dynamic_score({"status": "blocked", "criteria": {}}).score is None


def test_execution_request_uses_current_repository_without_notebook_assumption():
    from replicagent.dynamic import execution_request
    repository = {
        "archive_sha256": "abc", "source": "https://github.com/example/experiment",
        "file_paths": ["package/README.md", "package/bin/train.sh"],
        "source_excerpts": [{"path": "package/README.md", "text": "Run bash bin/train.sh"}],
        "local_archive_path": "/private/local/path",
    }
    request = execution_request(repository, {"title": "A different paper"}, {"summary": "Current review"})
    assert request["budget_seconds"] == 300
    assert request["context"]["repository"]["file_paths"] == repository["file_paths"]
    assert "notebook" not in request
    assert "local_archive_path" not in request["context"]["repository"]
    assert request["context"]["manifest"]["title"] == "A different paper"

"""Regression coverage for large repository and accumulated execution context."""
from copy import deepcopy
import json

from replicagent.execution_context import MAX_CONTEXT_BYTES, planner_contents


def render(context, attempts=None, extraction=None, *, compact=False):
    contents = planner_contents(
        context, attempts or [], 240,
        extraction or {"files": [], "skipped": [], "bytes": 0},
        allow_egress=True, compact=compact,
    )
    assert isinstance(contents, str)
    assert len(contents.encode("utf-8")) <= MAX_CONTEXT_BYTES
    value = json.loads(contents)
    assert set(value) >= {
        "remaining_seconds", "environment", "paper_and_repository",
        "extraction", "previous_attempts", "context_loading",
    }
    assert value["context_loading"]["full_record_path"] == "/workspace/.replicagent-audit/context.json"
    assert value["context_loading"]["selection_note"]
    return contents, value


def test_large_repository_inventory_fits_the_first_planner_request():
    paths = [
        f"release/generated/results/{'nested-directory/' * 8}{index:05d}/output.json"
        for index in range(40_000)
    ]
    paths.extend(["release/README.md", "release/tests/test_method.py"])
    context = {
        "manifest": {"title": "A paper with a large repository"},
        "review": {"summary": "Try the documented small test."},
        "repository": {
            "source": "https://github.com/example/large-paper",
            "file_paths": paths,
            "source_excerpts": [{
                "file": "release/README.md",
                "text": "Run python -m unittest discover -s tests to exercise the supplied method.",
            }],
        },
    }
    _, value = render(context, extraction={"files": paths, "skipped": [], "bytes": 1000})
    repository = value["paper_and_repository"]["repository"]
    inventory = repository["inventory"]
    assert inventory["total_files"] == len(paths)
    assert inventory["omitted_files"] == len(paths) - len(inventory["sample_paths"])
    assert inventory["omitted_files"] > 0
    assert "file_paths" not in repository
    assert "release/README.md" in inventory["sample_paths"]
    assert any("unittest discover" in item.get("text", "") for item in repository["source_excerpts"])
    assert value["extraction"]["total_files_extracted"] == len(paths)
    assert "files" not in value["extraction"]


def test_accumulated_output_keeps_the_latest_failure_without_resending_every_log():
    attempts = [{
        "argv": ["python", f"experiment_{index}.py"],
        "working_directory": "release",
        "purpose": "Run a supplied experiment.",
        "kind": "run",
        "exit_code": 0,
        "elapsed_seconds": 0.2,
        "stdout_tail": "Repeated numerical output.\n" * 2500,
    } for index in range(200)]
    attempts[-1]["exit_code"] = 1
    attempts[-1]["stdout_tail"] += "\nLATEST_FAILURE: FileNotFoundError: evaluation-input.csv\n"
    _, value = render({"repository": {"file_paths": [], "source_excerpts": []}}, attempts)
    history = value["previous_attempts"]
    assert history["total_attempts"] == 200
    assert len(history["recent"]) < len(attempts)
    assert any(
        "LATEST_FAILURE: FileNotFoundError: evaluation-input.csv" in item.get("stdout_tail", "")
        and item.get("exit_code") == 1
        for item in history["recent"]
    )
    assert len(history["older"]) > 0


def test_serialized_unicode_and_control_characters_cannot_exceed_the_request_bound():
    large = "🧪\x00\n" * 120_000
    context = {
        "manifest": {"title": large, "contribution": large},
        "review": large,
        "repository": {
            "source": large,
            "revision": large,
            "file_paths": [large, "release/README.md"],
            "source_excerpts": [{"file": "release/README.md", "text": large}],
        },
    }
    extraction = {
        "files": [large],
        "skipped": [{"path": large, "reason": large, "bytes": 1}],
        "bytes": 1,
    }
    _, value = render(context, extraction=extraction)
    assert value["extraction"]["skipped_count"] == 1
    assert "skipped_sample" in value["extraction"]


def test_context_selection_preserves_original_evidence_and_execution_records():
    context = {
        "manifest": {"title": "Current paper"},
        "review": {"reproduction_plan": [{"relevant_files": ["release/run.py"]}]},
        "repository": {
            "file_paths": ["release/run.py", "release/README.md"],
            "source_excerpts": [{"file": "release/README.md", "text": "Guide\n" * 20_000}],
        },
    }
    attempts = [{
        "argv": ["python", "run.py"], "kind": "run", "exit_code": 1,
        "stdout_tail": "Original failure details\n" * 4000,
    }]
    extraction = {
        "files": ["release/run.py"],
        "skipped": [{"path": "release/large-data.csv", "reason": "Size limit.", "bytes": 900_000_000}],
        "bytes": 200,
    }
    originals = deepcopy((context, attempts, extraction))
    render(context, attempts, extraction)
    assert (context, attempts, extraction) == originals


def test_compact_retry_uses_less_context_and_preserves_read_more_guidance():
    context = {
        "manifest": {"title": "A large but readable paper"},
        "review": {"summary": "A detailed static review. " * 2000},
        "repository": {
            "file_paths": [f"release/src/module_{index:05d}.py" for index in range(3000)],
            "source_excerpts": [{
                "file": f"release/src/module_{index}.py",
                "text": "def method(values):\n    return sum(values)\n" * 500,
            } for index in range(12)],
        },
    }
    attempts = [{
        "argv": ["python", "run.py"], "kind": "run", "exit_code": 1,
        "stdout_tail": "Progress\n" * 5000 + "Actual final error.",
    } for _ in range(12)]
    ordinary, _ = render(context, attempts)
    compact, value = render(context, attempts, compact=True)
    assert len(compact.encode("utf-8")) < len(ordinary.encode("utf-8"))
    assert any("Actual final error." in item.get("stdout_tail", "") for item in value["previous_attempts"]["recent"])


def test_context_record_does_not_follow_a_replaced_file_link(tmp_path):
    from replicagent.dynamic_worker import write_execution_record
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("Keep this file.")
    write_execution_record(workspace, {"title": "Original"}, {}, [])
    record = workspace / ".replicagent-audit/context.json"
    record.unlink()
    record.symlink_to(outside)
    write_execution_record(workspace, {"title": "Updated"}, {}, [])
    assert outside.read_text() == "Keep this file."
    assert json.loads(record.read_text())["context"]["title"] == "Updated"


def test_planner_retries_provider_context_overflow_without_setting_generation_caps(monkeypatch):
    from types import SimpleNamespace
    from replicagent import execution_planner as module
    calls = []
    context = {"repository": {
        "file_paths": [f"release/src/part_{i}.py" for i in range(2000)],
        "source_excerpts": [],
    }}
    class Client:
        def __init__(self, **kwargs):
            self.models = self
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("400 INVALID_ARGUMENT: The input token count exceeds the maximum.")
            return SimpleNamespace(parsed=module.ExecutionAction(argv=["python", "test.py"], kind="test"))
        def close(self):
            pass
    monkeypatch.setattr(module.genai, "Client", Client)
    action = module.next_action(context, [], 120, {"files": [], "skipped": []})
    assert action.argv == ["python", "test.py"]
    assert len(calls) == 2
    assert len(calls[1]["contents"]) < len(calls[0]["contents"])
    for call in calls:
        assert call["config"].max_output_tokens is None
        assert call["config"].thinking_config is None

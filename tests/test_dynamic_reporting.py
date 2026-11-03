"""Keep the final dynamic report brief while retaining observed outcomes."""
from copy import deepcopy
import re

from replicagent.reporting import render_execution


def summary(execution):
    text = "\n".join(render_execution(execution))
    assert len(text.split()) <= 220
    return text


def attempt(argv, *, kind="test", code=0, output="", timed_out=False):
    return {
        "argv": argv, "working_directory": "paper-release",
        "purpose": "Exercise the supplied implementation.",
        "kind": kind, "exit_code": code, "elapsed_seconds": 2.0,
        "timed_out": timed_out, "stdout_tail": output,
    }


def test_large_execution_record_becomes_a_short_summary_without_raw_logs():
    execution = {
        "status": "partially_verified", "elapsed_seconds": 260,
        "budget_seconds": 300,
        "reason": "I mathematically proved the entire paper. " * 200,
        "attempts": [
            attempt(["python", "-m", "pip", "install", "-r", "requirements.txt"],
                    kind="setup", output="Downloading_DISTINCT_PACKAGE_NOISE\n" * 3000),
            attempt(["python", "example.py"], kind="run",
                    output="NUMERICAL_PROGRESS_NOISE\n" * 3000 + "Saved 28 figures."),
        ],
    }
    text = summary(execution)
    assert "260" in text and "300" in text and "seconds" in text
    assert re.search(r"complet|succeed|success", text, re.IGNORECASE)
    assert "Downloading_DISTINCT_PACKAGE_NOISE" not in text
    assert "NUMERICAL_PROGRESS_NOISE" not in text
    assert "mathematically proved" not in text
    assert "#### Attempt" not in text
    assert re.search(r"unverified|unassessed|not verified|not assessed", text, re.IGNORECASE)


def test_broad_test_result_is_reported_once_without_adding_reruns_or_subsets():
    execution = {
        "status": "partially_verified", "elapsed_seconds": 260,
        "budget_seconds": 300,
        "attempts": [
            attempt(["pytest", "tests/test_simple_workflow.py"],
                    output="================ 6 passed in 2.75s ================"),
            attempt(["pytest"], code=1,
                    output="Traceback_DISTINCT_DETAIL\n" * 1000
                    + "================ 1 failed, 629 passed, 2 skipped in 36.48s ================"),
            attempt(["python", "-m", "pytest", "tests/test_metrics.py"],
                    output="================ 7 passed in 2.11s ================"),
            attempt(["pytest"], code=1,
                    output="================ 1 failed, 629 passed, 2 skipped in 36.48s ================"),
        ],
    }
    text = summary(execution)
    assert text.count("629") == 1
    assert re.search(r"1\s+failed|failed\s*:\s*1", text, re.IGNORECASE)
    assert re.search(r"2\s+skipped|skipped\s*:\s*2", text, re.IGNORECASE)
    assert "1258" not in text and "1271" not in text
    assert "Traceback_DISTINCT_DETAIL" not in text
    assert re.search(r"test.*report|report.*test", text, re.IGNORECASE | re.DOTALL)


def test_failure_and_timeout_are_visible_without_their_long_tracebacks():
    execution = {
        "status": "failed", "budget_seconds": 300, "elapsed_seconds": 289,
        "timed_out": True,
        "attempts": [
            attempt(["python", "evaluate.py"], kind="run", code=1,
                    output="Traceback (most recent call last):\n"
                    + "DISTINCT_STACK_FRAME\n" * 2000
                    + "FileNotFoundError: weights.bin"),
            attempt(["python", "train.py"], kind="run", code=124,
                    timed_out=True, output="DISTINCT_TRAINING_PROGRESS\n" * 2000),
        ],
    }
    text = summary(execution)
    assert re.search(r"fail|error|blocked", text, re.IGNORECASE)
    assert re.search(r"timed out|time limit|timeout", text, re.IGNORECASE)
    assert "DISTINCT_STACK_FRAME" not in text
    assert "DISTINCT_TRAINING_PROGRESS" not in text
    assert "Traceback (most recent call last)" not in text


def test_setup_and_empty_actions_do_not_claim_repository_execution():
    execution = {
        "status": "blocked", "budget_seconds": 300,
        "attempts": [
            attempt(["python", "-m", "pip", "install", "example"], kind="setup"),
            attempt(["ls", "-la"], kind="inspect"),
            attempt([], kind="run", code=None, output="The command was empty."),
        ],
    }
    text = summary(execution)
    assert "no repository code completed" in text.lower()


def test_repeated_analysis_commands_and_recovered_setup_stay_concise():
    execution = {
        "status": "partially_verified", "elapsed_seconds": 182,
        "budget_seconds": 300,
        "attempts": [
            attempt(["python", "-m", "pip", "install", "-e", "."], kind="setup",
                    code=1, output="OSError: No space left on device"),
            attempt(["python", "-m", "pip", "install", "--no-deps", "-e", "."],
                    kind="setup", output="Successfully installed the package."),
            attempt(["python", "scripts/analyse_results.py", "--derived-only"],
                    kind="run", code=1, output="ModuleNotFoundError: seaborn"),
            attempt(["python", "-m", "pip", "install", "seaborn"], kind="setup"),
            attempt(["/usr/local/bin/python", "scripts/analyse_results.py", "--derived-only"],
                    kind="run", output="Saved 28 figures."),
            attempt(["python3", "scripts/analyse_results.py", "--derived-only"],
                    kind="run", output="Saved 28 figures."),
        ],
    }
    text = summary(execution)
    assert re.search(r"complet|succeed|success", text, re.IGNORECASE)
    assert text.count("analyse_results.py") <= 2
    if "28 figures" in text:
        assert re.search(r"program.*report|output.*report|report.*output", text, re.IGNORECASE | re.DOTALL)


def test_summary_preserves_the_original_execution_record():
    execution = {
        "status": "partially_verified", "budget_seconds": 300,
        "attempts": [
            attempt(["pytest"], code=1,
                    output="1 failed, 629 passed, 2 skipped in 36.48s"),
            attempt(["python", "example.py"], kind="run",
                    output=chr(96) * 3 + "\n# UNTRUSTED_HEADING\n<script>UNTRUSTED_SCRIPT</script>"),
        ],
    }
    original = deepcopy(execution)
    text = summary(execution)
    assert execution == original
    assert "UNTRUSTED_HEADING" not in text
    assert "UNTRUSTED_SCRIPT" not in text
    assert "<script>" not in text


def test_summary_links_to_the_persisted_cloud_execution_record(monkeypatch):
    bucket = "test-project-replicagent"
    request_id = "a" * 32
    monkeypatch.setenv("REPLICAGENT_DYNAMIC_BUCKET", bucket)
    text = summary({
        "status": "partially_verified", "request_id": request_id,
        "attempts": [attempt(["pytest"], output="3 passed in 0.10s")],
    })
    assert (
        "https://console.cloud.google.com/storage/browser/_details/"
        + bucket + "/dynamic/" + request_id + "/result.json"
    ) in text


def test_summary_accepts_only_a_trusted_explicit_record_url(monkeypatch):
    monkeypatch.delenv("REPLICAGENT_DYNAMIC_BUCKET", raising=False)
    trusted = (
        "https://console.cloud.google.com/storage/browser/_details/"
        "test-project-replicagent/dynamic/" + "b" * 32 + "/result.json"
    )
    execution = {
        "status": "partially_verified", "record_url": trusted,
        "attempts": [attempt(["pytest"], output="3 passed in 0.10s")],
    }
    assert trusted in summary(execution)
    execution["record_url"] = "https://example.org/UNTRUSTED_LOG_DESTINATION"
    assert "UNTRUSTED_LOG_DESTINATION" not in summary(execution)

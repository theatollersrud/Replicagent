import stat
import time
import zipfile

import pytest

from replicagent.dynamic_worker import (
    BUDGET_SECONDS, child_exit_code, execute_repository, extract_archive, sandbox_exec_command,
    sandbox_start_command, summarize_attempts,
)
from replicagent.execution_planner import ExecutionAction


def outcome(code=0, text="", timed_out=False):
    return {"exit_code": code, "stdout_tail": text, "timed_out": timed_out, "elapsed_seconds": 0.1}


def test_normalizes_cloud_cli_child_exit_status_without_overriding_success():
    timeout_log = "Partial computation output.\nError: cmd.Wait(exec) failed: exit status 124\n"
    assert child_exit_code(1, timeout_log) == 124
    assert child_exit_code(1, "Error: cmd.Wait(exec) failed: exit status 7\n") == 7
    assert child_exit_code(0, timeout_log) == 0
    assert child_exit_code(None, timeout_log) is None
    assert child_exit_code(1, "Unrelated error: exit status 124\n") == 1
    assert child_exit_code(1, timeout_log + "Ordinary subsequent output.\n") == 1


def test_arbitrary_source_tree_is_preserved(tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("unrelated-paper/src/solve.py", "print(6 * 7)")
        bundle.writestr("unrelated-paper/tests/check.sh", "test 2 -eq 2")
        bundle.writestr("unrelated-paper/data/sample.csv", "x\n1\n")
    extraction = extract_archive(archive, tmp_path / "out")
    assert len(extraction["files"]) == 3
    assert extraction["skipped"] == []
    assert (tmp_path / "out/unrelated-paper/src/solve.py").read_text() == "print(6 * 7)"


def test_archive_rejects_symlink(tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        link = zipfile.ZipInfo("package/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(link, "/etc/passwd")
    with pytest.raises(ValueError, match="symbolic"):
        extract_archive(archive, tmp_path / "out")


def test_arbitrary_commands_remain_inside_sandbox(tmp_path):
    action = ExecutionAction(argv=["bash", "-lc", "./tests/check.sh"], working_directory="paper-code", kind="test")
    command = sandbox_exec_command("audit-123", action, 12)
    assert command[1] == "exec"
    assert "--workdir=/workspace/paper-code" in command
    assert "PATH=/usr/local/bin:/usr/bin:/bin" in command
    assert command[-3:] == action.argv
    assert "--allow-egress" not in sandbox_start_command(tmp_path, "audit-123")
    assert "--allow-egress" in sandbox_start_command(tmp_path, "audit-123", True)
    assert sum(x.startswith("--mount=") for x in sandbox_start_command(tmp_path, "audit-123")) == 1
    with pytest.raises(ValueError):
        sandbox_exec_command("audit-123", ExecutionAction(argv=["pwd"], working_directory="../../"), 12)


def test_agent_observes_failure_then_installs_and_retries(tmp_path):
    actions = iter([
        ExecutionAction(argv=["python", "experiment.py"], kind="run", purpose="Run the supplied experiment."),
        ExecutionAction(argv=["python", "-m", "pip", "install", "small-dependency"], kind="setup"),
        ExecutionAction(argv=["python", "experiment.py"], kind="run", purpose="Retry the supplied experiment."),
        ExecutionAction(done=True, reason="The original experiment completed after dependency installation."),
    ])
    calls = []
    feedback = []

    def planner(context, attempts, remaining, extraction, egress):
        feedback.append([dict(item) for item in attempts])
        return next(actions)

    def runner(command, seconds):
        calls.append(command)
        if command[1] != "exec":
            return outcome()
        executions = sum(call[1] == "exec" for call in calls)
        return outcome(1, "ModuleNotFoundError: small_dependency") if executions == 1 else outcome(0, "Computed result: 42")

    result = execute_repository(tmp_path, "test", {}, {}, time.monotonic() + 200, planner=planner, runner=runner)
    assert len(result["attempts"]) == 3
    assert "ModuleNotFoundError" in feedback[1][0]["stdout_tail"]
    assert result["status"] == "partially_verified"
    assert result["criteria"]["execution"]["fraction"] == 1
    assert "result_reproduction" not in result["criteria"]
    assert len(result["decisions"]) == 4
    assert calls[-1][1:3] == ["delete", "--force"]


def test_setup_and_inspection_do_not_receive_execution_points():
    result = summarize_attempts([
        {"kind": "setup", **outcome()},
        {"kind": "inspect", **outcome()},
    ], "Only setup completed.")
    assert result["status"] == "blocked"
    assert result["criteria"] == {}


def test_missing_prerequisite_is_unassessed_and_actual_test_failure_is_recorded():
    missing = summarize_attempts([{"kind": "run", **outcome(1, "ModuleNotFoundError: absent")}], "A dependency was unavailable.")
    failure = summarize_attempts([{"kind": "test", **outcome(1, "AssertionError: result differed")}], "An author test failed.")
    assert missing["criteria"] == {}
    assert failure["status"] == "failed"
    assert failure["criteria"]["execution"]["fraction"] == 0


def test_command_cannot_extend_shared_deadline(tmp_path):
    calls = []
    actions = iter([
        ExecutionAction(argv=["python", "long_run.py"], kind="run", timeout_seconds=285),
        ExecutionAction(done=True, reason="The attempt timed out."),
    ])

    def runner(command, seconds):
        calls.append((command, seconds))
        return outcome(124, "Still running", True) if command[1] == "exec" else outcome()

    result = execute_repository(tmp_path, "test", {}, {}, time.monotonic() + 8,
                                planner=lambda *args: next(actions), runner=runner)
    exec_call = next(call for call in calls if call[0][1] == "exec")
    assert exec_call[1] <= 8
    assert result["attempts"][0]["timeout_seconds"] < 8
    assert result["attempts"][0]["timed_out"]
    assert BUDGET_SECONDS == 300


def test_progress_failure_does_not_discard_evidence(tmp_path):
    actions = iter([ExecutionAction(argv=["python", "actual.py"], kind="run"), ExecutionAction(done=True)])

    def fail_publish(_):
        raise RuntimeError("Temporary storage outage.")

    result = execute_repository(tmp_path, "test", {}, {}, time.monotonic() + 30,
                                publish=fail_publish, planner=lambda *args: next(actions),
                                runner=lambda *args: outcome(0, "Real code completed"))
    assert result["status"] == "partially_verified"
    assert result["attempts"][0]["stdout_tail"] == "Real code completed"


def test_dead_execution_environment_stops_further_attempts(tmp_path):
    decisions = []
    def planner(*args):
        decisions.append(True)
        return ExecutionAction(argv=["python", "experiment.py"], kind="run")
    def runner(command, seconds):
        if command[1] == "exec":
            return outcome(1, 'urpc method "containerManager.WaitPID" failed: EOF')
        return outcome()
    result = execute_repository(tmp_path, "test", {}, {}, time.monotonic() + 30,
                                planner=planner, runner=runner)
    assert len(decisions) == 1
    assert len(result["attempts"]) == 1
    assert "does not establish its cause" in result["reason"]


def test_reading_a_result_file_is_inspection_even_if_planner_calls_it_a_test():
    result = summarize_attempts([
        {"argv": ["cat", "results.json"], "kind": "test", **outcome(0, "55")},
    ], "A stored result was read.")
    assert result["attempts"][0]["kind"] == "inspect"
    assert result["criteria"] == {}

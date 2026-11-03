from replicagent.reporting import render_execution


def test_execution_report_explains_each_arbitrary_command_and_outcome():
    execution = {
        "status": "partially_verified",
        "budget_seconds": 300,
        "elapsed_seconds": 28.5,
        "attempts": [
            {
                "argv": ["python", "-m", "pip", "install", "-r", "requirements.txt"],
                "working_directory": "paper-release",
                "purpose": "Install the dependencies listed by this package.",
                "kind": "setup",
                "exit_code": 0,
                "elapsed_seconds": 20.0,
                "stdout_tail": "Successfully installed example-package.",
            },
            {
                "argv": ["bash", "experiments/check-small-example.sh", "--seed", "7"],
                "working_directory": "paper-release",
                "purpose": "Run the supplied small example with its documented seed.",
                "kind": "run",
                "exit_code": 0,
                "elapsed_seconds": 8.5,
                "stdout_tail": "Computed residual: 0.00031\nSaved example-output.csv",
            },
        ],
    }
    markdown = "\n".join(render_execution(execution))
    assert "300 seconds" in markdown
    assert "Attempt 1: setup" in markdown
    assert "Attempt 2: run" in markdown
    assert "bash experiments/check-small-example.sh --seed 7" in markdown
    assert "`paper-release`" in markdown
    assert "Computed residual: 0.00031" in markdown
    assert "Setup and probe success alone" in markdown
    assert "notebook" not in markdown.lower()


def test_execution_report_distinguishes_a_failed_command_from_a_timeout():
    markdown = "\n".join(render_execution({
        "status": "failed", "budget_seconds": 300, "timed_out": True,
        "attempts": [
            {"argv": ["python", "evaluate.py"], "kind": "run", "exit_code": 1,
             "stdout_tail": "FileNotFoundError: weights.bin", "elapsed_seconds": 0.7},
            {"argv": ["python", "train.py"], "kind": "run", "exit_code": 124,
             "timed_out": True, "stdout_tail": "Finished epoch 2", "elapsed_seconds": 299.3},
        ],
    }))
    assert "failed with exit code 1" in markdown
    assert "The command timed out" in markdown
    assert "FileNotFoundError: weights.bin" in markdown
    assert "Finished epoch 2" in markdown


def test_execution_report_keeps_program_output_in_an_indented_block():
    markdown = "\n".join(render_execution({
        "status": "partially_verified",
        "attempts": [{"argv": ["python", "example.py"], "exit_code": 0,
                      "stdout_tail": "```\n# Untrusted heading\n<script>example</script>"}],
    }))
    assert "\n    ```\n    # Untrusted heading\n    <script>example</script>" in markdown

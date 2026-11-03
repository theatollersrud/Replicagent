"""Prepare synthetic packages locally and execute them only in Google Cloud.

The fixtures exercise different repository layouts through the normal execution
agent. The request contains source observations, not an execution-plan override.
Google credentials remain in this orchestration process and are never uploaded.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import quote
import uuid
import zipfile

import httpx


FIXTURES = {
    "shell_example": {
        "title": "A synthetic finite-series calculation",
        "files": {
            "cedar-release/README.md": (
                "# Finite-series example\n"
                "This package computes the sum of squares from 1 through 5.\n"
                "From the cedar-release directory, run: bash tools/run-example.sh\n"
                "The calculation prints sum_of_squares=55 and writes results/series.json.\n"
                "It uses Python's standard library and needs no external data.\n"
            ),
            "cedar-release/tools/run-example.sh": (
                "#!/bin/sh\nset -eu\npython calculation.py\n"
            ),
            "cedar-release/calculation.py": (
                "import json\nfrom pathlib import Path\n"
                "value = sum(n*n for n in range(1, 6))\n"
                "Path('results').mkdir(exist_ok=True)\n"
                "Path('results/series.json').write_text(json.dumps({'sum_of_squares': value}))\n"
                "assert value == 55\nprint(f'sum_of_squares={value}', flush=True)\n"
            ),
        },
        "expected_output": "sum_of_squares=55",
        "expect_success": True,
    },
    "dependency_tests": {
        "title": "A synthetic grouping implementation with unit tests",
        "files": {
            "harbor-code/README.md": (
                "# Grouping implementation\n"
                "The implementation groups observations into pairs.\n"
                "From harbor-code, first try python -m unittest discover -s checks -v.\n"
                "If dependencies are missing, install them with python -m pip install -r requirements.txt, then rerun the tests.\n"
                "The two tests cover an even and an odd number of observations.\n"
                "No external dataset or credentials are required.\n"
            ),
            "harbor-code/requirements.txt": "boltons==25.0.0\n",
            "harbor-code/grouping.py": (
                "from boltons.iterutils import chunked\n"
                "def pairs(values):\n    return list(chunked(values, 2))\n"
            ),
            "harbor-code/checks/test_grouping.py": (
                "import unittest\nfrom grouping import pairs\n"
                "class GroupingTests(unittest.TestCase):\n"
                "    def test_even(self):\n        self.assertEqual(pairs([1, 2, 3, 4]), [[1, 2], [3, 4]])\n"
                "    def test_odd(self):\n        self.assertEqual(pairs([1, 2, 3]), [[1, 2], [3]])\n"
            ),
        },
        "expected_output": "Ran 2 tests",
        "expect_success": True,
    },
    "runtime_failure": {
        "title": "A synthetic package with an observable runtime failure",
        "files": {
            "marsh-experiment/README.md": (
                "# Array calculation\n"
                "Run python evaluate.py from marsh-experiment to evaluate the supplied observations.\n"
                "The implementation uses Python's standard library.\n"
            ),
            "marsh-experiment/evaluate.py": (
                "observations = [1, 2, 3]\nweights = [1, 2]\n"
                "print('Evaluating observations with the supplied weights.', flush=True)\n"
                "if len(observations) != len(weights):\n"
                "    raise ValueError('observation and weight lengths differ')\n"
                "print(sum(x*w for x, w in zip(observations, weights)))\n"
            ),
        },
        "expected_output": "observation and weight lengths differ",
        "expect_success": False,
    },
    "timeout_command": {
        "title": "A synthetic calculation with a bounded smoke command",
        "files": {
            "willow-workload/README.md": (
                "# Bounded calculation smoke test\n"
                "The complete calculation takes about 30 seconds.\n"
                "For this repository's documented smoke test, run the following from willow-workload:\n"
                "timeout --signal=TERM --kill-after=1s 1s python workload.py\n"
                "The expected smoke behavior is to print Computation started, then stop at the one-second command limit with exit code 124.\n"
                "This smoke command checks startup and termination rather than the completed result.\n"
            ),
            "willow-workload/workload.py": (
                "import time\n"
                "print('Computation started', flush=True)\n"
                "deadline = time.monotonic() + 30\n"
                "total = 0\n"
                "while time.monotonic() < deadline:\n"
                "    total += sum(n*n for n in range(1000))\n"
                "print(f'Computation completed: {total}', flush=True)\n"
            ),
        },
        "expected_output": "Computation started",
        "expect_success": False,
        "expect_timeout": True,
    },
}


def access_token():
    return subprocess.check_output(
        ["gcloud", "auth", "print-access-token"], text=True, timeout=30
    ).strip()


def package_request(name, fixture):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in fixture["files"].items():
            archive.writestr(path, content)
    payload = output.getvalue()
    source = "synthetic-cloud-check://" + name
    request = {
        "archive_sha256": hashlib.sha256(payload).hexdigest(),
        "budget_seconds": 300,
        "context": {
            "manifest": {
                "title": fixture["title"],
                "contribution": "This is a synthetic execution fixture, not a real research result.",
                "claims": [],
            },
            "repository": {
                "source": source,
                "file_paths": list(fixture["files"]),
                "source_excerpts": [
                    {"file": path, "text": content}
                    for path, content in fixture["files"].items()
                ],
            },
            "review": {"summary": "Attempt the supplied package within the cloud execution budget."},
        },
    }
    return payload, request


def check_fixture(args, name):
    fixture = FIXTURES[name]
    payload, request = package_request(name, fixture)
    request_id = uuid.uuid4().hex
    prefix = "dynamic/" + request_id + "/"
    headers = {"Authorization": "Bearer " + access_token()}
    with httpx.Client(headers=headers, timeout=30) as client:
        for filename, data, content_type in (
            ("package.zip", payload, "application/zip"),
            ("request.json", json.dumps(request).encode(), "application/json"),
        ):
            response = client.post(
                f"https://storage.googleapis.com/upload/storage/v1/b/{args.bucket}/o",
                params={"uploadType": "media", "name": prefix + filename},
                content=data, headers={"Content-Type": content_type},
            )
            response.raise_for_status()
        job = f"projects/{args.project}/locations/{args.region}/jobs/{args.job}"
        response = client.post("https://run.googleapis.com/v2/" + job + ":run", json={
            "overrides": {"containerOverrides": [{"env": [{"name": "REQUEST_ID", "value": request_id}]}]},
        })
        response.raise_for_status()
        operation = response.json()["name"]
        print(f"{name}: The cloud job started with request {request_id}.", flush=True)
        deadline = time.monotonic() + 450
        result_url = f"https://storage.googleapis.com/storage/v1/b/{args.bucket}/o/{quote(prefix + 'result.json', safe='')}"
        while time.monotonic() < deadline:
            response = client.get(result_url, params={"alt": "media"})
            if response.status_code == 200:
                result = response.json()
                break
            if response.status_code != 404:
                response.raise_for_status()
            time.sleep(3)
        else:
            raise TimeoutError(f"The cloud worker did not return {name} within 450 seconds.")
    attempts = result.get("attempts", [])
    observed = [a for a in attempts if fixture["expected_output"] in a.get("stdout_tail", "")]
    completed = [a for a in observed if a.get("exit_code") == 0 and a.get("kind") in ("run", "test")]
    failed = [a for a in observed if a.get("exit_code") not in (None, 0)]
    passed = bool(completed if fixture["expect_success"] else failed)
    expectation = "The package completed and printed the expected evidence." if fixture["expect_success"] else "The original package exposed its runtime failure in command output."
    if name == "dependency_tests":
        successful_setup = [a for a in attempts if a.get("kind") == "setup" and a.get("exit_code") == 0]
        passed = bool(completed and successful_setup)
        expectation = "A dependency setup command completed, followed by successful execution of both unit tests."
    if fixture.get("expect_timeout"):
        timed_out = [a for a in observed if a.get("timed_out") is True and a.get("exit_code") == 124]
        passed = bool(timed_out)
        expectation = "The workload printed its startup message, then the worker recorded timed_out=true and exit code 124."
    record = {
        "fixture": name, "request_id": request_id, "operation": operation,
        "passed": passed, "result": result,
        "expectation": expectation,
    }
    args.output_directory.mkdir(parents=True, exist_ok=True)
    (args.output_directory / (name + ".json")).write_text(json.dumps(record, indent=2))
    print(f"{name}: {'PASS' if passed else 'FAIL'} with {len(attempts)} command attempts and status {result.get('status')}.", flush=True)
    for index, attempt in enumerate(attempts, 1):
        print(f"{name} attempt {index}: {attempt.get('kind')} {attempt.get('argv')} exited {attempt.get('exit_code')}.", flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="agentic-oslo-team-12-9afd")
    parser.add_argument("--region", default="europe-west4")
    parser.add_argument("--job", default="replicagent-dynamic")
    parser.add_argument("--bucket", default="agentic-oslo-team-12-9afd-replicagent")
    parser.add_argument("--case", choices=["all", *FIXTURES], default="all")
    parser.add_argument("--output-directory", type=Path, default=Path("/tmp/replicagent-dynamic-checks"))
    args = parser.parse_args()
    names = list(FIXTURES) if args.case == "all" else [args.case]
    results = []
    with ThreadPoolExecutor(max_workers=len(names)) as executor:
        pending = {executor.submit(check_fixture, args, name): name for name in names}
        for future in as_completed(pending):
            name = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:
                print(f"{name}: The cloud check failed: {type(exc).__name__}: {exc}", flush=True)
                results.append({"fixture": name, "passed": False, "error": str(exc)})
    args.output_directory.mkdir(parents=True, exist_ok=True)
    (args.output_directory / "summary.json").write_text(json.dumps(results, indent=2))
    raise SystemExit(0 if all(result["passed"] for result in results) else 1)


if __name__ == "__main__":
    main()

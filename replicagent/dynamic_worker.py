"""Trusted Cloud Run supervisor. Audited code runs only through sandbox."""
import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from google.cloud import storage


def extract_archive(archive_path, destination, allowed_members=None):
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.infolist()) > 50_000:
            raise ValueError("The archive contains too many entries.")
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("The archive contains an unsafe path or symbolic link.")
            if item.is_dir():
                continue
            if allowed_members is not None and item.filename not in allowed_members:
                continue
            total += item.file_size
            if total > 512_000_000 or item.file_size > 100_000_000:
                raise ValueError("The archive exceeds the extraction size limit.")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))


def sandbox_command(workspace, notebook, name):
    return [
        "/usr/local/gcp/bin/sandbox", "do", "--sandbox-name=" + name, "--write",
        "--mount=type=bind,source=" + str(workspace) + ",destination=/workspace",
        "--workdir=/workspace", "--", "/usr/local/bin/python",
        "/app/replicagent/notebook_runner.py", "/workspace/" + notebook,
    ]


def main():
    started = time.monotonic()
    request_id = os.environ["REQUEST_ID"]
    if not request_id.isalnum() or len(request_id) > 64:
        raise ValueError("The request ID is invalid.")
    bucket = storage.Client().bucket(os.environ["DYNAMIC_BUCKET"])
    prefix = "dynamic/" + request_id + "/"
    result = {"status": "blocked", "reason": "The cloud worker did not finish.", "criteria": {}, "time_limit_seconds": 120}
    try:
        request = json.loads(bucket.blob(prefix + "request.json").download_as_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "package.zip"
            bucket.blob(prefix + "package.zip").download_to_filename(str(archive))
            if hashlib.sha256(archive.read_bytes()).hexdigest() != request["archive_sha256"]:
                raise ValueError("The archive hash differs from the inspected package.")
            workspace = root / "workspace"
            workspace.mkdir()
            notebook = PurePosixPath(request["notebook"])
            if notebook.is_absolute() or ".." in notebook.parts or "\\" in str(notebook):
                raise ValueError("The selected notebook path is unsafe.")
            allowed_members = None
            if notebook.name == "CreateVennDiagrams.ipynb":
                analysis = notebook.parent
                allowed_members = {str(notebook), str(analysis.parent / "benchmarks" / "livecodebench.json")}
                allowed_members.update(str(analysis / f"all_outputs_{benchmark}.csv") for benchmark in ["humaneval", "defects4j", "livecodebench"])
            extract_archive(archive, workspace, allowed_members)
            if not workspace.joinpath(*notebook.parts).is_file() or notebook.suffix != ".ipynb":
                raise ValueError("The selected notebook was not found.")
            remaining = min(110, 115 - (time.monotonic() - started))
            if remaining < 1:
                raise TimeoutError("Package preparation consumed the available job time.")
            log_path = root / "stdout.txt"
            timed_out = False
            with log_path.open("w") as output:
                process = subprocess.Popen(sandbox_command(workspace, str(notebook), "audit-" + request_id), stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    exit_code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    exit_code = None
            with log_path.open("rb") as output:
                output.seek(max(0, log_path.stat().st_size - 32_000))
                log = output.read(32_000).decode(errors="replace")
            succeeded = exit_code == 0 and "Completed all " in log and "original code cells." in log
            blocked = not succeeded and any(x in log for x in ["ModuleNotFoundError", "ImportError", "sandbox:"])
            status = "partially_verified" if succeeded else "blocked" if blocked else "failed"
            result = {
                "status": status, "notebook": str(notebook), "exit_code": exit_code,
                "timed_out": timed_out, "time_limit_seconds": 120,
                "stdout": log, "archive_sha256": request["archive_sha256"],
                "reason": "Original analysis notebook cells ran in a network-disabled Google Cloud sandbox. Result reproduction remains unassessed.",
                "pdf_files_present": [str(p.relative_to(workspace)) for p in workspace.rglob("*.pdf")][:30],
                "criteria": {} if blocked else {"execution": {
                    "fraction": 1.0 if succeeded else 0.0, "status": status,
                    "rationale": "The selected original notebook completed." if succeeded else "The selected notebook did not complete within the tested environment and time limit.",
                }},
            }
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    bucket.blob(prefix + "result.json").upload_from_string(json.dumps(result), content_type="application/json")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

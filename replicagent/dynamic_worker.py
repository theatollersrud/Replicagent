"""Trusted cloud supervisor. All repository commands execute inside a sandbox."""
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from google.cloud import storage

from .execution_planner import ExecutionAction, next_action

BUDGET_SECONDS = 300
SAVE_RESERVE_SECONDS = 15
SANDBOX = "/usr/local/gcp/bin/sandbox"
LOG_TAIL_BYTES = 64_000


def write_execution_record(workspace, context, extraction, attempts):
    """Replace the sandbox's evidence copy without following sandbox-created links."""
    directory = workspace / ".replicagent-audit"
    directory.mkdir(exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".context-" + uuid.uuid4().hex
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w") as output:
            json.dump({"context": context, "extraction": extraction, "attempts": attempts},
                      output, ensure_ascii=False)
        os.replace(temporary, "context.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def child_exit_code(supervisor_exit_code, output):
    """Cloud Run's CLI reports child failures as exit 1 plus a final error line."""
    if supervisor_exit_code in (None, 0):
        return supervisor_exit_code
    match = re.search(r"(?:^|\n)Error: cmd\.Wait\(exec\) failed: exit status (\d+)\s*\Z", output)
    return int(match.group(1)) if match else supervisor_exit_code


def safe_relative_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise ValueError("The command working directory must remain inside /workspace.")
    return path


def extract_archive(archive_path, destination, allowed_members=None):
    """Keep arbitrary source trees, and describe files omitted for resource limits."""
    total = 0
    extracted = []
    skipped = []
    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.infolist()) > 50_000:
            raise ValueError("The archive contains too many entries.")
        # Small source files and manifests remain available even when large data
        # files exhaust the extraction allowance.
        for item in sorted(archive.infolist(), key=lambda entry: entry.file_size):
            path = safe_relative_path(item.filename)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("The archive contains a symbolic link or special file.")
            if item.is_dir():
                continue
            if allowed_members is not None and item.filename not in allowed_members:
                skipped.append({"path": item.filename, "reason": "Not selected for extraction."})
                continue
            if item.file_size > 100_000_000 or total + item.file_size > 512_000_000:
                skipped.append({"path": item.filename, "reason": "The file exceeds the cloud extraction size allowance.", "bytes": item.file_size})
                continue
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Reading is bounded by the declared ZIP entry size and total above.
            target.write_bytes(archive.read(item))
            target.chmod(0o755 if mode & 0o111 else 0o644)
            total += item.file_size
            extracted.append(item.filename)
    return {"files": extracted, "skipped": skipped, "bytes": total}


def sandbox_start_command(workspace, name, allow_egress=False):
    command = [
        SANDBOX, "run", "--detach", "--write",
        "--mount=type=bind,source=" + str(workspace) + ",destination=/workspace",
        "--workdir=/workspace",
    ]
    if allow_egress:
        command.append("--allow-egress")
    return command + [name, "--", "/bin/sleep", str(BUDGET_SECONDS)]


def sandbox_exec_command(name, action, seconds):
    relative = safe_relative_path(action.working_directory)
    if not action.argv or any(not isinstance(value, str) or "\x00" in value for value in action.argv):
        raise ValueError("The selected command has no valid executable.")
    workdir = "/workspace" if str(relative) == "." else "/workspace/" + str(relative)
    return [
        SANDBOX, "exec", "--workdir=" + workdir, name, "--",
        "/usr/bin/env", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp",
        "MPLBACKEND=Agg", "PYTHONUNBUFFERED=1",
        "/usr/bin/timeout", "--signal=TERM", "--kill-after=1s", str(seconds) + "s",
        *action.argv,
    ]


def run_process(command, seconds):
    """Drain output continuously while retaining a bounded tail, avoiding disk fills."""
    started = time.monotonic()
    tail = bytearray()
    count = 0
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)

    def drain():
        nonlocal count
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                break
            count += len(chunk)
            tail.extend(chunk)
            if len(tail) > LOG_TAIL_BYTES:
                del tail[:-LOG_TAIL_BYTES]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        code = process.wait(timeout=max(0.1, seconds))
        timed_out = code in (124, 137)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        code = None
    finally:
        reader.join(timeout=1)
    output = bytes(tail).decode(errors="replace")
    child_code = child_exit_code(code, output)
    return {
        "exit_code": child_code, "supervisor_exit_code": code,
        "timed_out": timed_out or child_code in (124, 137),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stdout_tail": output,
        "output_bytes": count, "output_truncated": count > LOG_TAIL_BYTES,
    }


def summarize_attempts(attempts, reason, timed_out=False):
    # The planner can mislabel reading a result file as a test. A successful
    # read-only command is still inspection and receives no execution credit.
    inspection_commands = {"cat", "head", "tail", "find", "ls", "pwd", "which", "stat", "du", "df", "grep", "rg", "echo", "printf"}
    for attempt in attempts:
        argv = attempt.get("argv", [])
        if argv and PurePosixPath(argv[0]).name in inspection_commands:
            attempt["kind"] = "inspect"
    meaningful = [a for a in attempts if a.get("kind") in ("run", "test")]
    successful = [a for a in meaningful if a.get("exit_code") == 0 and not a.get("timed_out")]
    blockers = ("ModuleNotFoundError", "ImportError", "No such file or directory", "not found", "Permission denied", "No module named", "CUDA", "ConnectionError", "Network is unreachable")
    blocked = not successful and (not meaningful or all(any(token in a.get("stdout_tail", "") for token in blockers) for a in meaningful))
    status = "partially_verified" if successful else "blocked" if blocked else "failed"
    criteria = {}
    if meaningful and not blocked:
        criteria["execution"] = {
            "fraction": 1.0 if successful else 0.0,
            "status": status,
            "rationale": (
                f"{len(successful)} of {len(meaningful)} attempted code runs or tests completed successfully. This establishes execution of that subset only."
                if successful else "The attempted code did not complete successfully in the available environment and time."
            ),
        }
    return {
        "status": status, "reason": reason,
        "budget_seconds": BUDGET_SECONDS, "time_limit_seconds": BUDGET_SECONDS,
        "timed_out": timed_out, "attempts": attempts, "criteria": criteria,
        "result_reproduction": "Unassessed. Executing code alone does not verify the paper's reported results.",
    }


def execute_repository(workspace, name, context, extraction, deadline, publish=None, planner=next_action, runner=run_process):
    """Make and revise execution choices until the shared cloud deadline."""
    allow_egress = os.getenv("SANDBOX_ALLOW_EGRESS", "").lower() == "true"
    attempts = []
    decisions = []
    reason = "The five-minute cloud time budget was exhausted."
    timed_out = False

    def update(value):
        if publish:
            try:
                publish({**value, "status": "running", "revision": time.time()})
            except Exception:
                # A progress-write failure must not discard execution evidence.
                pass

    # Full evidence remains available for targeted reads. Only a selected view
    # is sent to the language model, so large repositories remain executable.
    write_execution_record(workspace, context, extraction, attempts)

    try:
        startup = runner(sandbox_start_command(workspace, name, allow_egress), min(15, max(0.1, deadline - time.monotonic())))
        if startup["exit_code"] != 0:
            return summarize_attempts([], "The isolated execution environment could not start: " + startup["stdout_tail"])
        while time.monotonic() < deadline - 2:
            remaining = deadline - time.monotonic()
            try:
                action = planner(context, attempts, remaining, extraction, allow_egress)
                if isinstance(action, dict):
                    action = ExecutionAction.model_validate(action)
                decisions.append({**action.model_dump(), "remaining_seconds": round(remaining, 1)})
            except Exception as exc:
                reason = f"The execution planner could not choose another command: {type(exc).__name__}: {str(exc)[:500]}"
                break
            if action.done:
                reason = action.reason or "The execution agent finished its available useful attempts."
                break
            remaining = deadline - time.monotonic()
            if remaining < 2:
                timed_out = True
                break
            seconds = max(1, min(action.timeout_seconds, int(remaining - 1)))
            attempt = {
                "argv": action.argv, "working_directory": action.working_directory,
                "purpose": action.purpose, "kind": action.kind,
                "timeout_seconds": seconds,
            }
            try:
                command = sandbox_exec_command(name, action, seconds)
                update({"message": f"I am attempting: {action.purpose}", "current": attempt, "attempts": attempts, "remaining_seconds": round(remaining, 1)})
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    timed_out = True
                    break
                attempt.update(runner(command, min(seconds + 1.5, remaining)))
            except Exception as exc:
                attempt.update({"exit_code": None, "timed_out": False, "elapsed_seconds": 0, "stdout_tail": f"Command launch failed: {type(exc).__name__}: {exc}"})
            attempts.append(attempt)
            try:
                write_execution_record(workspace, context, extraction, attempts)
            except OSError:
                # A modified sandbox copy cannot discard host-side observations.
                pass
            outcome = "timed out" if attempt["timed_out"] else f"exited with code {attempt['exit_code']}"
            update({"message": f"The command {outcome}. I am assessing the output and remaining time.", "attempts": attempts, "remaining_seconds": round(max(0, deadline - time.monotonic()), 1)})
            if attempt.get("exit_code") != 0 and (
                "is not running" in attempt.get("stdout_tail", "")
                or 'urpc method "containerManager.WaitPID" failed: EOF' in attempt.get("stdout_tail", "")
            ):
                reason = "The isolated execution environment stopped. Earlier command results are retained. The supervisor output records the failure, but does not establish its cause."
                break
        else:
            timed_out = True
    finally:
        # Force deletion also stops children left behind by a timed-out command.
        try:
            runner([SANDBOX, "delete", "--force", name], 3)
        except Exception:
            pass
    result = summarize_attempts(attempts, reason, timed_out)
    result["network_access"] = "outbound" if allow_egress else "disabled"
    result["decisions"] = decisions
    return result


def main():
    started = time.monotonic()
    deadline = started + BUDGET_SECONDS - SAVE_RESERVE_SECONDS
    request_id = os.environ["REQUEST_ID"]
    if not request_id.isalnum() or len(request_id) > 64:
        raise ValueError("The request ID is invalid.")
    bucket = storage.Client().bucket(os.environ["DYNAMIC_BUCKET"])
    prefix = "dynamic/" + request_id + "/"
    result = summarize_attempts([], "The cloud worker did not finish.")

    def publish(value):
        bucket.blob(prefix + "progress.json").upload_from_string(json.dumps(value), content_type="application/json", timeout=2, retry=None)

    try:
        request = json.loads(bucket.blob(prefix + "request.json").download_as_text(timeout=10, retry=None))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "package.zip"
            bucket.blob(prefix + "package.zip").download_to_filename(str(archive), timeout=30, retry=None)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != request["archive_sha256"]:
                raise ValueError("The archive hash differs from the statically inspected package.")
            workspace = root / "workspace"
            workspace.mkdir()
            extraction = extract_archive(archive, workspace)
            result = execute_repository(workspace, "audit-" + request_id, request.get("context", {}), extraction, deadline, publish)
            result["archive_sha256"] = request["archive_sha256"]
            result["extraction"] = {"files_extracted": len(extraction["files"]), "bytes": extraction["bytes"], "skipped": extraction["skipped"]}
    except Exception as exc:
        result["reason"] = f"Cloud execution was blocked: {type(exc).__name__}: {str(exc)[:500]}"
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    bucket.blob(prefix + "result.json").upload_from_string(json.dumps(result), content_type="application/json", timeout=5, retry=None)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

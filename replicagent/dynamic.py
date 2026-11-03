"""Submit the current paper's package to a bounded cloud execution agent."""
import json
import os
import time
import uuid
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from .storage import EvidenceLedger, read, save

BUDGET_SECONDS = 300


def execution_request(repository, manifest, review):
    """Pass observations, never a paper-specific executable or filename."""
    return {
        "archive_sha256": repository["archive_sha256"],
        "budget_seconds": BUDGET_SECONDS,
        "context": {
            "manifest": manifest,
            "review": review,
            "repository": {key: repository[key] for key in (
                "source", "revision", "file_paths", "source_excerpts", "facts",
            ) if key in repository},
        },
    }


def run_dynamic(audit_id, review=None, on_progress=None):
    result = {"status": "blocked", "criteria": {}, "budget_seconds": BUDGET_SECONDS,
              "reason": "The cloud execution worker is not configured."}
    job = os.getenv("REPLICAGENT_DYNAMIC_JOB")
    repository = read(audit_id, "repository.json")
    if not job or repository.get("status") != "inspected" or not repository.get("local_archive_path"):
        if job:
            result["reason"] = "Artifact inspection did not produce a downloadable code package."
        save(audit_id, "execution.json", result)
        return result
    try:
        credentials, project = google.auth.default()
        bucket = storage.Client(project=project, credentials=credentials).bucket(os.environ["REPLICAGENT_DYNAMIC_BUCKET"])
        request_id = uuid.uuid4().hex
        prefix = "dynamic/" + request_id + "/"
        bucket.blob(prefix + "package.zip").upload_from_filename(
            str(Path(repository["local_archive_path"])), timeout=60)
        request = execution_request(repository, read(audit_id, "manifest.json"), review or {})
        bucket.blob(prefix + "request.json").upload_from_string(
            json.dumps(request), content_type="application/json", timeout=30)
        session = AuthorizedSession(credentials)
        response = session.post("https://run.googleapis.com/v2/" + job + ":run", json={
            "overrides": {"containerOverrides": [{"env": [{"name": "REQUEST_ID", "value": request_id}]}]},
        }, timeout=30)
        response.raise_for_status()
        operation = response.json()["name"]
        result_blob = bucket.blob(prefix + "result.json")
        progress_blob = bucket.blob(prefix + "progress.json")
        # Queueing and result retrieval are outside the five-minute worker budget.
        deadline = time.monotonic() + BUDGET_SECONDS + 120
        last_message = None
        last_progress = {}
        while time.monotonic() < deadline:
            if result_blob.exists(timeout=10):
                result = json.loads(result_blob.download_as_text(timeout=10))
                break
            if progress_blob.exists(timeout=10):
                last_progress = json.loads(progress_blob.download_as_text(timeout=10))
                message = last_progress.get("message")
                if message and message != last_message and on_progress:
                    on_progress(message)
                    last_message = message
            time.sleep(3)
        else:
            result = {
                "status": "blocked", "criteria": {}, "budget_seconds": BUDGET_SECONDS,
                "attempts": last_progress.get("attempts", []),
                "reason": "The cloud job did not return its final result. Its task has a hard five-minute timeout. Any available command observations are shown below.",
            }
        result.update(operation=operation, request_id=request_id)
        record = EvidenceLedger(audit_id).add(
            "cloud_execution", repository["source"], "Agent-selected repository commands",
            json.dumps(result, sort_keys=True),
            "The execution agent selected commands from the current package and observed their output inside an isolated Cloud Run sandbox. Cloud Run enforces a 300-second task timeout with no retries.")
        for criterion in result.get("criteria", {}).values():
            criterion["evidence_ids"] = [record.evidence_id]
    except Exception as exc:
        result = {"status": "blocked", "criteria": {}, "budget_seconds": BUDGET_SECONDS,
                  "reason": f"Cloud execution was blocked: {type(exc).__name__}: {str(exc)[:500]}"}
    save(audit_id, "execution.json", result)
    return result

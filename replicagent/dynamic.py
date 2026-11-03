"""Request a bounded cloud job. This process never executes repository code."""
import json
import os
import time
import uuid
from pathlib import Path
import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from .storage import EvidenceLedger, read, save


def choose_notebook(repository):
    notebooks = [p for p in repository.get("file_paths", []) if p.endswith(".ipynb") and "analysis/" in p.lower()]
    return min(notebooks, key=lambda p: (0 if p.endswith("/CreateVennDiagrams.ipynb") else 1, len(p), p)) if notebooks else None


def run_dynamic(audit_id):
    result = {"status": "blocked", "criteria": {}, "reason": "The cloud execution worker is not configured."}
    job = os.getenv("REPLICAGENT_DYNAMIC_JOB")
    repository = read(audit_id, "repository.json")
    notebook = choose_notebook(repository)
    if not job or repository.get("status") != "inspected" or not notebook:
        result["reason"] = "No supported analysis notebook was found." if job else result["reason"]
        save(audit_id, "execution.json", result)
        return result
    try:
        credentials, project = google.auth.default()
        bucket = storage.Client(project=project, credentials=credentials).bucket(os.environ["REPLICAGENT_DYNAMIC_BUCKET"])
        request_id = uuid.uuid4().hex
        prefix = "dynamic/" + request_id + "/"
        bucket.blob(prefix + "package.zip").upload_from_filename(str(Path(repository["local_archive_path"])))
        bucket.blob(prefix + "request.json").upload_from_string(json.dumps({
            "notebook": notebook, "archive_sha256": repository["archive_sha256"],
        }), content_type="application/json")
        response = AuthorizedSession(credentials).post("https://run.googleapis.com/v2/" + job + ":run", json={
            "overrides": {"containerOverrides": [{"env": [{"name": "REQUEST_ID", "value": request_id}]}]},
        }, timeout=30)
        response.raise_for_status()
        operation = response.json()["name"]
        result_blob = bucket.blob(prefix + "result.json")
        deadline = time.monotonic() + 170
        while time.monotonic() < deadline:
            if result_blob.exists():
                result = json.loads(result_blob.download_as_text())
                break
            time.sleep(3)
        else:
            result = {"status": "blocked", "criteria": {}, "reason": "The cloud job did not return a result. Its task has a hard 120-second timeout."}
        result["operation"] = operation
        result["request_id"] = request_id
        record = EvidenceLedger(audit_id).add("cloud_execution", repository["source"], notebook, json.dumps(result, sort_keys=True), "Ran original notebook cells inside a network-disabled Cloud Run sandbox. Cloud Run enforces a 120-second task timeout, with no retries.")
        for criterion in result.get("criteria", {}).values():
            criterion["evidence_ids"] = [record.evidence_id]
    except Exception as exc:
        result = {"status": "blocked", "criteria": {}, "reason": f"Cloud execution was blocked: {type(exc).__name__}: {str(exc)[:500]}"}
    save(audit_id, "execution.json", result)
    return result

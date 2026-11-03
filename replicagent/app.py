"""Optional local API. The ADK web interface remains the primary demo entry point."""

import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .schemas import AuditRequest
from .services import public_url
from .storage import AUDITS, read, save

app = FastAPI(title="Replicagent", version="0.1.0")

INDEX = Path(__file__).with_name("static") / "index.html"
MAX_UPLOAD = 25 * 1024 * 1024


def last_message(session) -> str | None:
    for event in reversed(getattr(session, "events", None) or []):
        for part in (event.content.parts if event.content else None) or []:
            if part.text:
                return part.text
    return None


async def execute_upload(audit_id: str, upload: bytes, repository_url: str | None):
    try:
        from scripts.run_audit import run
        session = await run("uploaded-paper.pdf", repository_url, audit_id, upload)
    except Exception as exc:
        save(audit_id, "status.json", {"audit_id": audit_id, "state": "blocked", "stage": "The audit stopped", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
        return
    # The workflow can stop before the audit starts, for example when no paper is identified.
    if read(audit_id, "status.json")["state"] == "queued":
        save(audit_id, "status.json", {"audit_id": audit_id, "state": "blocked", "stage": "The audit did not start", "error": last_message(session) or "The workflow stopped before reading the paper."})


async def execute(audit_id: str, request: AuditRequest):
    try:
        if request.mock:
            import asyncio
            from .mock import run_mock
            await asyncio.to_thread(run_mock, audit_id)
        else:
            from scripts.run_audit import run
            await run(request.paper_source, request.repository_url, audit_id)
    except Exception as exc:
        save(audit_id, "status.json", {"audit_id": audit_id, "state": "blocked", "stage": "The audit stopped", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})


def load(audit_id: str, filename: str):
    if not (AUDITS / audit_id / "status.json").exists():
        raise HTTPException(404, "The audit was not found.")
    try:
        return read(audit_id, filename)
    except FileNotFoundError:
        raise HTTPException(404, "This audit result is not available yet.")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX, media_type="text/html")


@app.post("/audits/upload", status_code=202)
async def upload_audit(request: Request, tasks: BackgroundTasks, repository_url: str | None = None):
    """Accept a raw PDF body, so the page needs no multipart handling."""
    upload = await request.body()
    if len(upload) > MAX_UPLOAD:
        raise HTTPException(413, "The PDF exceeds the 25 MB limit.")
    if not upload.startswith(b"%PDF-"):
        raise HTTPException(415, "The upload is not a PDF file.")
    if repository_url:
        repository_url = repository_url.strip()
        try:
            public_url(repository_url)
        except Exception:
            raise HTTPException(422, "The repository URL must be a public https URL.")
    audit_id = uuid.uuid4().hex[:12]
    save(audit_id, "request.json", {"paper_source": "uploaded-paper.pdf", "filename": request.headers.get("x-filename", "")[:200], "repository_url": repository_url, "bytes": len(upload)})
    save(audit_id, "status.json", {"audit_id": audit_id, "state": "queued", "stage": "Waiting to start", "error": None})
    tasks.add_task(execute_upload, audit_id, upload, repository_url or None)
    return {"audit_id": audit_id, "status_url": f"/audits/{audit_id}"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/audits", status_code=202)
async def create_audit(request: AuditRequest, tasks: BackgroundTasks):
    audit_id = uuid.uuid4().hex[:12]
    save(audit_id, "request.json", request.model_dump())
    save(audit_id, "status.json", {"audit_id": audit_id, "state": "queued", "stage": "Waiting to start", "error": None})
    tasks.add_task(execute, audit_id, request)
    return {"audit_id": audit_id, "status_url": f"/audits/{audit_id}"}


@app.get("/audits/{audit_id}")
def status(audit_id: str):
    return load(audit_id, "status.json")


@app.get("/audits/{audit_id}/claims")
def claims(audit_id: str):
    return load(audit_id, "manifest.json")["claims"]


@app.get("/audits/{audit_id}/findings")
def findings(audit_id: str):
    return load(audit_id, "report.json")["findings"]


@app.get("/audits/{audit_id}/executions")
def executions(audit_id: str):
    load(audit_id, "status.json")
    return {"status": "not_attempted", "executions": [], "reason": "Isolated execution is outside this demo."}


@app.get("/audits/{audit_id}/score")
def score(audit_id: str):
    report = load(audit_id, "report.json")
    return {"static_score": report["static_score"], "dynamic_score": report["dynamic_score"]}


@app.get("/audits/{audit_id}/report")
def report(audit_id: str, format: str = "json"):
    data = load(audit_id, "report.json")
    if format == "markdown":
        return PlainTextResponse((AUDITS / audit_id / "report.md").read_text(encoding="utf-8"))
    return data

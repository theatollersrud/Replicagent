"""Optional local API. The ADK web interface remains the primary demo entry point."""

import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from .schemas import AuditRequest
from .storage import AUDITS, read, save

app = FastAPI(title="Replicagent", version="0.1.0")


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
    return load(audit_id, "report.json")["score"]


@app.get("/audits/{audit_id}/report")
def report(audit_id: str, format: str = "json"):
    data = load(audit_id, "report.json")
    if format == "markdown":
        return PlainTextResponse((AUDITS / audit_id / "report.md").read_text(encoding="utf-8"))
    return data

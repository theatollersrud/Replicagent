"""The local drag-and-drop page and its upload endpoint. No model is called."""

import pytest
from fastapi.testclient import TestClient

import scripts.run_audit
from replicagent import storage
from replicagent.app import app
from replicagent.mock import run_mock


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")
    monkeypatch.setattr("replicagent.app.AUDITS", tmp_path / "audits")


def test_index_page_is_served():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Drop a paper PDF here" in response.text


def test_upload_rejects_non_pdf_bodies():
    client = TestClient(app)
    assert client.post("/audits/upload", content=b"hello", headers={"Content-Type": "application/pdf"}).status_code == 415
    assert client.post("/audits/upload?repository_url=http://127.0.0.1/x", content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}).status_code == 422


def test_upload_runs_the_workflow_with_the_pdf_and_serves_the_report(monkeypatch):
    received = {}

    async def fake_run(source, repository, audit_id, upload):
        received.update(source=source, repository=repository, upload=upload)
        run_mock(audit_id)

    monkeypatch.setattr(scripts.run_audit, "run", fake_run)
    client = TestClient(app)
    response = client.post("/audits/upload", content=b"%PDF-1.7 synthetic", headers={"Content-Type": "application/pdf", "X-Filename": "paper.pdf"})
    assert response.status_code == 202
    audit_id = response.json()["audit_id"]
    assert received == {"source": "uploaded-paper.pdf", "repository": None, "upload": b"%PDF-1.7 synthetic"}
    assert client.get(f"/audits/{audit_id}").json()["state"] == "completed_static_audit"
    assert client.get(f"/audits/{audit_id}/report").json()["paper"]["title"] == "Synthetic offline audit fixture"


def test_failed_upload_audit_is_reported_as_blocked(monkeypatch):
    async def broken_run(source, repository, audit_id, upload):
        raise ValueError("The demo supports papers of up to 80 pages.")

    monkeypatch.setattr(scripts.run_audit, "run", broken_run)
    client = TestClient(app)
    audit_id = client.post("/audits/upload", content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}).json()["audit_id"]
    status = client.get(f"/audits/{audit_id}").json()
    assert status["state"] == "blocked"
    assert "80 pages" in status["error"]


def test_workflow_that_stops_before_the_audit_is_reported_as_blocked(monkeypatch):
    from types import SimpleNamespace
    from google.genai import types

    async def stopped_run(source, repository, audit_id, upload):
        message = types.Content(role="model", parts=[types.Part(text="I could not identify a PDF in the supplied attachments.")])
        return SimpleNamespace(events=[SimpleNamespace(content=message)])

    monkeypatch.setattr(scripts.run_audit, "run", stopped_run)
    client = TestClient(app)
    audit_id = client.post("/audits/upload", content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}).json()["audit_id"]
    status = client.get(f"/audits/{audit_id}").json()
    assert status["state"] == "blocked"
    assert "could not identify a PDF" in status["error"]

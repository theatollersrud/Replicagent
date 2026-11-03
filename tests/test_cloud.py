from fastapi.testclient import TestClient


def test_cloud_entry_point_has_one_agent_and_the_report_api(monkeypatch, tmp_path):
    monkeypatch.delenv("REPLICAGENT_RUNTIME_NAME", raising=False)
    monkeypatch.setenv("ADK_SESSION_SERVICE_URI", "memory://")
    monkeypatch.setenv("ADK_ARTIFACT_SERVICE_URI", "file://" + str(tmp_path / "artifacts"))
    from replicagent.cloud import create_app
    with TestClient(create_app()) as client:
        assert client.get("/audit-api/health").json() == {"status": "ok"}
        assert client.get("/list-apps").json() == ["replicagent"]


def test_managed_playground_routes_use_the_adk_session_service(monkeypatch):
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from replicagent.cloud import create_app

    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (AnonymousCredentials(), "demo-project"))
    monkeypatch.setenv("REPLICAGENT_RUNTIME_NAME", "projects/123/locations/europe-west4/reasoningEngines/456")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "europe-west4")
    monkeypatch.setenv("ADK_SESSION_SERVICE_URI", "memory://")
    monkeypatch.setenv("ADK_ARTIFACT_SERVICE_URI", "memory://")
    with TestClient(create_app()) as client:
        response = client.post("/api/reasoning_engine", json={
            "class_method": "async_list_sessions", "input": {"user_id": "cloud-test"},
        })
        assert response.status_code == 200, response.text
        assert response.json()["output"]["sessions"] == []

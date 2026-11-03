import asyncio
from types import SimpleNamespace

import pytest
from google.adk.agents import LlmAgent
from google.adk.events import Event, EventActions
from google.genai import types

from replicagent import auth, runtime, storage
from replicagent.runtime import AuditLlmAgent
from replicagent.schemas import PaperManifest


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")


def context():
    return SimpleNamespace(session=SimpleNamespace(state={"audit_id": "recovery-test"}))


def successful_output():
    return Event(author="UnderstandPaper", content=types.Content(role="model", parts=[types.Part(text="A complete result was returned.")]), actions=EventActions(state_delta={"paper_manifest": {"validated": True}}))


async def collect(agent):
    return [event async for event in agent._run_async_impl(context())]


def test_invalid_json_is_retried_before_the_next_stage(monkeypatch):
    calls = []
    async def fake_model(self, ctx):
        calls.append(1)
        if len(calls) == 1:
            PaperManifest.model_validate_json('{"title":"synthetic_private_payload')
        yield successful_output()
    monkeypatch.setattr(LlmAgent, "_run_async_impl", fake_model)
    agent = AuditLlmAgent(name="UnderstandPaper", output_key="paper_manifest")
    events = asyncio.run(collect(agent))
    assert len(calls) == 2
    assert "retrying" in events[0].content.parts[0].text
    assert storage.read("recovery-test", "status.json")["state"] == "running"
    assert "synthetic_private_payload" not in storage.read("recovery-test", "UnderstandPaper-attempt-1-error.json")["message"]


def test_authentication_failure_refreshes_and_retries(monkeypatch):
    class AuthenticationError(Exception):
        code = 401
    calls, refreshes = [], []
    async def fake_model(self, ctx):
        calls.append(1)
        if len(calls) == 1:
            raise AuthenticationError("401 UNAUTHENTICATED")
        yield successful_output()
    monkeypatch.setattr(LlmAgent, "_run_async_impl", fake_model)
    monkeypatch.setattr(runtime, "refresh_credentials", lambda: refreshes.append(1))
    asyncio.run(collect(AuditLlmAgent(name="UnderstandPaper", output_key="paper_manifest")))
    assert len(calls) == 2
    assert len(refreshes) == 1


def test_repeated_invalid_output_records_blocked_state(monkeypatch):
    async def fake_model(self, ctx):
        PaperManifest.model_validate_json('{"title":"incomplete')
        yield successful_output()
    monkeypatch.setattr(LlmAgent, "_run_async_impl", fake_model)
    with pytest.raises(RuntimeError, match="Extracting paper claims stopped"):
        asyncio.run(collect(AuditLlmAgent(name="UnderstandPaper", output_key="paper_manifest")))
    status = storage.read("recovery-test", "status.json")
    assert status["state"] == "blocked"
    assert "structured response" in status["error"]


def test_empty_output_stops_before_using_missing_state(monkeypatch):
    async def fake_model(self, ctx):
        if False:
            yield successful_output()
    monkeypatch.setattr(LlmAgent, "_run_async_impl", fake_model)
    with pytest.raises(RuntimeError, match="no validated structured output"):
        asyncio.run(collect(AuditLlmAgent(name="UnderstandPaper", output_key="paper_manifest")))
    assert storage.read("recovery-test", "status.json")["state"] == "blocked"


def test_gcloud_credentials_do_not_assume_an_extra_lifetime(monkeypatch):
    from google.genai._api_client import get_token_from_credentials
    calls = []
    monkeypatch.setattr(auth.subprocess, "check_output", lambda *args, **kwargs: (calls.append(1) or "synthetic-token"))
    credentials = auth.GcloudCredentials()
    assert get_token_from_credentials(None, credentials) == "synthetic-token"
    assert get_token_from_credentials(None, credentials) == "synthetic-token"
    assert len(calls) == 2


def test_model_configuration_keeps_reasoning_and_output_defaults(monkeypatch):
    from google.auth.credentials import AnonymousCredentials
    monkeypatch.setattr(auth, "local_credentials", lambda: AnonymousCredentials())
    from replicagent.agent import understand, review
    for agent in (understand, review):
        assert agent.generate_content_config.max_output_tokens is None
        assert agent.generate_content_config.thinking_config is None

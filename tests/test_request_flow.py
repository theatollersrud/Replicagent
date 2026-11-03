"""Check paper selection and stopping behavior through the actual ADK graph."""

import asyncio
import json

import pytest
from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import Workflow
from google.auth.credentials import AnonymousCredentials
from google.genai import types


@pytest.fixture
def agents(monkeypatch, tmp_path):
    from replicagent import auth, storage
    monkeypatch.setattr(auth, "local_credentials", lambda: AnonymousCredentials())
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")
    from replicagent import agent
    return agent


async def send(runner, text):
    return [event async for event in runner.run_async(
        user_id="input-test", session_id="input-session",
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    )]


def test_missing_and_unreadable_inputs_end_before_model_calls(agents, monkeypatch):
    async def unexpected_model(self, ctx):
        raise AssertionError("The model must not run without a readable paper.")
        yield
    monkeypatch.setattr(agents.AuditLlmAgent, "_run_async_impl", unexpected_model)
    def unreadable(*args):
        raise ValueError("The response was HTML instead of a paper.")
    monkeypatch.setattr(agents, "ingest_paper", unreadable)

    async def scenario():
        sessions = InMemorySessionService()
        await sessions.create_session(app_name="replicagent", user_id="input-test", session_id="input-session", state={
            "requested_paper_source": "https://arxiv.org/abs/2510.21513",
            "paper_context": "previous paper", "report_id": "previous", "static_score": {"score": 7},
        })
        runner = Runner(agent=agents.root_agent, app_name="replicagent", session_service=sessions)
        events = await send(runner, "Hello, what can you do?")
        assert events and {e.author for e in events} == {"IngestPaper"}
        state = (await sessions.get_session(app_name="replicagent", user_id="input-test", session_id="input-session")).state
        assert state["paper_context"] is None and state["report_id"] is None
        assert state["static_score"] is None and state["requested_paper_source"] is None
        events = await send(runner, "Audit https://example.org/download?id=second-paper")
        assert events and {e.author for e in events} == {"IngestPaper"}
        assert "could not read" in events[-1].content.parts[0].text
    asyncio.run(scenario())


def test_successive_papers_in_one_session_use_each_current_source(agents, monkeypatch):
    seen = []
    def ingest(audit_id, source, upload):
        seen.append(source)
        return {"source": source, "format": "pdf", "pages": 1, "evidence": []}
    monkeypatch.setattr(agents, "ingest_paper", ingest)

    class Continue(BaseAgent):
        async def _run_async_impl(self, ctx):
            yield agents.progress(self.name, json.loads(ctx.session.state["paper_context"])["source"])

    async def scenario():
        first = agents.IngestPaper(name="ReadSelectedPaper")
        next_stage = Continue(name="NextStage")
        workflow = Workflow(name="SelectionTest", edges=[("START", first), (first, {"ready": next_stage})])
        sessions = InMemorySessionService()
        await sessions.create_session(app_name="input-test", user_id="input-test", session_id="input-session")
        runner = Runner(agent=workflow, app_name="input-test", session_service=sessions)
        for source in ["https://arxiv.org/abs/1706.03762", "https://example.org/paper.pdf?download=1"]:
            events = await send(runner, "Audit " + source)
            assert any(e.author == "NextStage" and e.content.parts[0].text == source for e in events)
        assert seen == ["https://arxiv.org/abs/1706.03762", "https://example.org/paper.pdf?download=1"]
    asyncio.run(scenario())

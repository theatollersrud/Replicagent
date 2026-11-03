"""Run the same ADK workflow from a terminal, without the web interface."""

import argparse
import asyncio
import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from replicagent.agent import root_agent, static_agent


async def run(source: str, repository: str | None = None, audit_id: str | None = None, static_only: bool = False):
    sessions = InMemorySessionService()
    session_id = uuid.uuid4().hex
    state = {"requested_paper_source": source, "requested_repository_url": repository, "run_dynamic": not static_only}
    if audit_id:
        state["requested_audit_id"] = audit_id
    await sessions.create_session(app_name="replicagent", user_id="demo", session_id=session_id, state=state)
    runner = Runner(agent=static_agent if static_only else root_agent, app_name="replicagent", session_service=sessions)
    async for event in runner.run_async(
        user_id="demo", session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=f"Audit {source}")]),
    ):
        if event.content:
            for part in event.content.parts or []:
                if part.text:
                    print(f"[{event.author}] {part.text}", flush=True)
    return await sessions.get_session(app_name="replicagent", user_id="demo", session_id=session_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", default="https://arxiv.org/abs/2510.21513v2")
    parser.add_argument("--repository")
    parser.add_argument("--static-only", action="store_true", help="Skip execution, generated checks, reference checks, and the correctness review.")
    args = parser.parse_args()
    asyncio.run(run(args.source, args.repository, static_only=args.static_only))

"""Run the same ADK workflow from a terminal, without the web interface."""

import argparse
import asyncio
import hashlib
import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from replicagent.agent import root_agent


async def run(source: str, repository: str | None = None, audit_id: str | None = None, upload: bytes | None = None):
    sessions = InMemorySessionService()
    session_id = uuid.uuid4().hex
    state = {"requested_paper_source": source, "requested_repository_url": repository}
    if audit_id:
        state["requested_audit_id"] = audit_id
    if upload:
        state["requested_upload_sha256"] = hashlib.sha256(upload).hexdigest()
    await sessions.create_session(app_name="replicagent", user_id="demo", session_id=session_id, state=state)
    runner = Runner(agent=root_agent, app_name="replicagent", session_service=sessions)
    parts = [types.Part(text=f"Audit {source}")]
    if upload:
        # IngestPaper reads an inline PDF in place of the source URL.
        parts.append(types.Part(inline_data=types.Blob(mime_type="application/pdf", data=upload)))
    async for event in runner.run_async(
        user_id="demo", session_id=session_id,
        new_message=types.Content(role="user", parts=parts),
    ):
        if event.content:
            for part in event.content.parts or []:
                if part.text:
                    print(f"[{event.author}] {part.text}", flush=True)
    return await sessions.get_session(app_name="replicagent", user_id="demo", session_id=session_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="The paper URL or local paper path to audit.")
    parser.add_argument("--repository")
    args = parser.parse_args()
    asyncio.run(run(args.source, args.repository))

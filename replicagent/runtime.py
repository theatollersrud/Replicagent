"""Recover from model failures and persist the actual audit state."""

import asyncio
import json

from google.adk.agents import LlmAgent
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import ValidationError

from .auth import refresh_credentials
from .storage import save


def record_stage(audit_id: str, stage: str, *, error: str | None = None):
    save(audit_id, "status.json", {
        "audit_id": audit_id, "state": "blocked" if error else "running",
        "stage": stage, "error": error,
    })


def error_description(error: Exception) -> str:
    if isinstance(error, ValidationError):
        details = error.errors(include_input=False, include_url=False)
        return "The model returned an incomplete or invalid structured response: " + json.dumps(details)[:600]
    if getattr(error, "code", None) == 401 or "401 UNAUTHENTICATED" in str(error):
        return "Google Cloud rejected the model credentials. Fresh credentials were requested before retrying."
    return f"{type(error).__name__}: {str(error)[:600]}"


class AuditLlmAgent(LlmAgent):
    """Retry invalid structured output or expired authentication once."""

    async def _run_async_impl(self, ctx):
        audit_id = ctx.session.state["audit_id"]
        stage = {"paper_manifest": "Extracting paper claims", "review": "Reviewing the observed evidence", "correctness": "Assessing correctness"}.get(self.output_key, "Reviewing the observed evidence")
        for attempt in range(2):
            record_stage(audit_id, stage)
            try:
                produced_output = False
                async for event in super()._run_async_impl(ctx):
                    if event.actions.state_delta.get(self.output_key) is not None:
                        produced_output = True
                    yield event
                if not produced_output:
                    raise RuntimeError("The model returned no validated structured output.")
                return
            except Exception as error:
                authentication_error = getattr(error, "code", None) == 401 or "401 UNAUTHENTICATED" in str(error)
                invalid_output = isinstance(error, (ValidationError, json.JSONDecodeError))
                message = error_description(error)
                save(audit_id, f"{self.name}-attempt-{attempt + 1}-error.json", {
                    "stage": stage, "attempt": attempt + 1,
                    "error_type": type(error).__name__, "message": message,
                })
                if attempt == 0 and (authentication_error or invalid_output):
                    if authentication_error:
                        try:
                            await asyncio.to_thread(refresh_credentials)
                        except Exception as refresh_error:
                            record_stage(audit_id, stage, error=error_description(refresh_error))
                            raise
                    yield Event(
                        author=self.name,
                        content=types.Content(role="model", parts=[types.Part(text=f"{message} I am retrying this stage.")]),
                        actions=EventActions(state_delta={self.output_key: None}),
                    )
                    continue
                record_stage(audit_id, stage, error=message)
                raise RuntimeError(f"{stage} stopped. {message} The audit ID is {audit_id}.") from error

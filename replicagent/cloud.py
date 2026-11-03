"""Cloud entry point for the ADK interface, managed Playground, and report API."""

import os

import uvicorn
from google.adk.cli.fast_api import get_fast_api_app

from .app import app as audit_api
from .storage import ROOT


def create_app():
    runtime_name = os.getenv("REPLICAGENT_RUNTIME_NAME")
    app = get_fast_api_app(
        agents_dir=str(ROOT), web=True, use_local_storage=False,
        session_service_uri=os.getenv("ADK_SESSION_SERVICE_URI", f"agentengine://{runtime_name}" if runtime_name else "memory://"),
        artifact_service_uri=os.getenv("ADK_ARTIFACT_SERVICE_URI", "memory://"),
        gemini_enterprise_app_name="replicagent" if runtime_name else None,
        host="0.0.0.0", port=int(os.getenv("PORT", "8080")),
        max_llm_calls=0,
    )
    app.mount("/audit-api", audit_api)
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

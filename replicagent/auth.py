"""The local demo reuses an existing Google login and stores no secrets."""

import os
import subprocess
import shutil
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone

import google.auth
from google.auth.credentials import Credentials
from google.adk.models.google_llm import Gemini
from google.genai import types


class GcloudCredentials(Credentials):
    def refresh(self, request):
        self.token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True, timeout=30
        ).strip()
        # gcloud owns the real token lifetime. Ask it again for every request
        # instead of claiming that its current token has another 45 minutes.
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None)


@lru_cache(maxsize=1)
def local_credentials():
    adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if shutil.which("gcloud") and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and not adc.exists():
        return GcloudCredentials()
    return google.auth.default()[0]


def refresh_credentials():
    from google.auth.transport.requests import Request
    local_credentials().refresh(Request())


def model():
    credentials = local_credentials()
    return Gemini(
        model=os.getenv("AUDITOR_MODEL", "gemini-3.5-flash"),
        client_kwargs={
            "vertexai": True,
            "project": os.getenv("GOOGLE_CLOUD_PROJECT", "agentic-oslo-team-12-9afd"),
            "location": os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            "credentials": credentials,
        },
        retry_options=types.HttpRetryOptions(attempts=3, initial_delay=2, max_delay=10),
    )

import importlib.metadata
import subprocess
from datetime import datetime, timedelta

import google.auth
from google.auth.credentials import Credentials
from google.genai import Client, types


class GcloudCredentials(Credentials):
    """Use the existing gcloud login without writing a token to disk."""

    def refresh(self, request):
        self.token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True, timeout=30
        ).strip()
        self.expiry = datetime.utcnow() + timedelta(minutes=45)


if __name__ == "__main__":
    try:
        credentials, _ = google.auth.default()
        print("Using application default credentials.")
    except google.auth.exceptions.DefaultCredentialsError:
        credentials = GcloudCredentials()
        print("Using the existing gcloud login.")
    client = Client(
        vertexai=True,
        project="agentic-oslo-team-12-9afd",
        location="global",
        credentials=credentials,
        http_options=types.HttpOptions(timeout=60000),
    )
    for model in ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash-lite"]:
        try:
            response = client.models.generate_content(
                model=model,
                contents="Reply with exactly: Model access works.",
                config=types.GenerateContentConfig(max_output_tokens=128),
            )
            print(model, response.text)
            break
        except Exception as exc:
            print(model, type(exc).__name__, str(exc)[:350])
    print("ADK", importlib.metadata.version("google-adk"))

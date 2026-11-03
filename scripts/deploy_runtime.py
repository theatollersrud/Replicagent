"""Deploy a Cloud Build image to the shared Google Cloud Agent Runtime Playground."""

import argparse
import logging
import subprocess

import agentplatform
import google.auth
from google.auth.credentials import Credentials
from google.adk.cli.cli_deploy import _AGENT_ENGINE_CLASS_METHODS


class GcloudCredentials(Credentials):
    """Reuse the deploying user's gcloud login without writing access tokens."""

    def refresh(self, request):
        self.token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True, timeout=30,
        ).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="europe-west4")
    parser.add_argument("--image", required=True)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--artifact-bucket", required=True)
    parser.add_argument("--runtime-name", help="Update an existing runtime instead of creating one.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        credentials, _ = google.auth.default()
    except google.auth.exceptions.DefaultCredentialsError:
        credentials = GcloudCredentials()
    client = agentplatform.Client(project=args.project, location=args.region, credentials=credentials)
    runtime_name = args.runtime_name
    if not runtime_name:
        runtime = client.runtimes.create(config={"display_name": "Replicagent"})
        runtime_name = runtime.api_resource.name
        print(f"Runtime resource: {runtime_name}", flush=True)
    client.runtimes.update(name=runtime_name, config={
        "display_name": "Replicagent",
        "description": "Research paper reproducibility auditor. Static and dynamic scores are reported separately.",
        "agent_framework": "google-adk",
        "service_account": args.service_account,
        "class_methods": _AGENT_ENGINE_CLASS_METHODS,
        "container_spec": {"image_uri": args.image},
        "min_instances": 1, "max_instances": 1,
        "resource_limits": {"cpu": "2", "memory": "4Gi"},
        "container_concurrency": 4,
        "env_vars": {
            "AUDITOR_MODEL_LOCATION": "global",
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            "REPLICAGENT_RUNTIME_NAME": runtime_name,
            "ADK_ARTIFACT_SERVICE_URI": f"gs://{args.artifact_bucket}",
            "AUDITOR_MODEL": "gemini-3.5-flash",
        },
    })
    runtime_id = runtime_name.rsplit("/", 1)[-1]
    print(f"Playground: https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/{args.region}/agent-engines/{runtime_id}/playground?project={args.project}")


if __name__ == "__main__":
    main()

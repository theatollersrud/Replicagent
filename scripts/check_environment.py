"""Check model access through the same client configuration as the workflow."""

from importlib.metadata import version

from replicagent.auth import model


if __name__ == "__main__":
    configured_model = model()
    response = configured_model.api_client.models.generate_content(
        model=configured_model.model,
        contents="Reply with exactly: Model access works.",
    )
    print(response.text)
    print("ADK", version("google-adk"))

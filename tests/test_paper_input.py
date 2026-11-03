import asyncio
from types import SimpleNamespace

import pytest
from google.genai import types

from replicagent.paper_input import PaperInputError, select_paper


def context(text="", *, parts=(), state=None, artifacts=None):
    return SimpleNamespace(
        user_content=types.Content(role="user", parts=[types.Part(text=text), *parts]),
        session=SimpleNamespace(id="session-a", app_name="replicagent", user_id="user-a", state=state or {}),
        app_name="replicagent", user_id="user-a", artifact_service=artifacts,
    )


def choose(ctx):
    return asyncio.run(select_paper(ctx))


@pytest.mark.parametrize("text,expected", [
    ("Audit https://example.org/download?id=42", "https://example.org/download?id=42"),
    ("Audit https://example.org/paper.pdf?download=1.", "https://example.org/paper.pdf?download=1"),
    ("Audit http://example.org/paper.pdf", "https://example.org/paper.pdf"),
    ("Read arxiv.org/pdf/2610.00430v2.pdf", "https://arxiv.org/abs/2610.00430v2"),
    ("Audit arXiv:2610.00430.", "https://arxiv.org/abs/2610.00430"),
    ("Audit 2610.00430v2", "https://arxiv.org/abs/2610.00430v2"),
    ("Audit [this paper](https://arxiv.org/abs/2610.00430)", "https://arxiv.org/abs/2610.00430"),
])
def test_current_request_identifies_paper(text, expected):
    assert choose(context(text)).source == expected


@pytest.mark.parametrize("text", ["Hello", "Tell me what you do", "Review the paper Attention Is All You Need", "https://github.com/example/repo"])
def test_missing_paper_never_reuses_demo_or_stale_source(text):
    old = {"requested_paper_source": "https://arxiv.org/abs/2510.21513", "requested_audit_id": "old", "requested_repository_url": "https://github.com/old/repo"}
    with pytest.raises(PaperInputError):
        choose(context(text, state=old))


def test_new_paper_and_repository_replace_stale_request_state():
    old = {"requested_paper_source": "old.tex", "requested_audit_id": "old", "requested_repository_url": "https://github.com/old/repo"}
    selected = choose(context("Audit https://arxiv.org/abs/2610.00430 using https://github.com/new/repo", state=old))
    assert selected.source == "https://arxiv.org/abs/2610.00430"
    assert selected.repository_url == "https://github.com/new/repo"
    assert selected.requested_audit_id is None


def test_cli_protocol_allows_explicit_local_source_and_one_request_metadata():
    state = {"requested_paper_source": "inputs/custom.tex", "requested_audit_id": "request-1", "requested_repository_url": "https://github.com/new/repo"}
    selected = choose(context("Audit inputs/custom.tex", state=state))
    assert selected.source == "inputs/custom.tex"
    assert selected.requested_audit_id == "request-1"
    assert selected.repository_url == "https://github.com/new/repo"
    with pytest.raises(PaperInputError):
        choose(context("Please audit inputs/custom.tex", state=state))


def test_api_bare_arxiv_identifier_preserves_its_requested_audit_id():
    selected = choose(context("Audit 2610.00430", state={"requested_paper_source": "2610.00430", "requested_audit_id": "api-request"}))
    assert selected.source == "https://arxiv.org/abs/2610.00430"
    assert selected.requested_audit_id == "api-request"


@pytest.mark.parametrize("mime", ["application/pdf", "application/octet-stream"])
def test_uploaded_pdf_takes_current_input_instead_of_stale_source(mime):
    part = types.Part(inline_data=types.Blob(mime_type=mime, data=b"%PDF-1.7 actual supplied paper"))
    selected = choose(context("Review this attachment", parts=[part], state={"requested_paper_source": "old.tex"}))
    assert selected.upload == part.inline_data.data
    assert selected.source == "uploaded-paper.pdf"


@pytest.mark.parametrize("uri", [
    "https://example.org/download?id=42",
    "gs://project-bucket/uploads/paper.pdf",
])
def test_referenced_pdf_is_the_requested_source(uri):
    part = types.Part(file_data=types.FileData(file_uri=uri, mime_type="application/pdf"))
    selected = choose(context("Review this", parts=[part]))
    assert selected.source == uri
    assert selected.upload is None


def test_multiple_papers_are_rejected_and_duplicate_links_are_deduplicated():
    with pytest.raises(PaperInputError, match="more than one"):
        choose(context("Audit https://arxiv.org/abs/2610.00430 and https://arxiv.org/abs/2510.21513"))
    selected = choose(context("https://arxiv.org/abs/2610.00430 https://arxiv.org/pdf/2610.00430"))
    assert selected.source == "https://arxiv.org/abs/2610.00430"


class Artifacts:
    def __init__(self):
        self.calls = []

    async def load_artifact(self, **kwargs):
        self.calls.append(kwargs)
        return types.Part(inline_data=types.Blob(mime_type="application/pdf", data=b"%PDF-1.7 saved paper"))


@pytest.mark.parametrize("placeholder", [
    '[Uploaded Artifact: "paper.pdf"]',
    "Uploaded file: paper.pdf. It is saved into artifacts",
])
def test_saved_upload_is_loaded_from_current_session(placeholder):
    service = Artifacts()
    selected = choose(context(placeholder, artifacts=service))
    assert selected.upload == b"%PDF-1.7 saved paper"
    assert service.calls == [{"app_name": "replicagent", "user_id": "user-a", "session_id": "session-a", "filename": "paper.pdf", "version": None}]


def test_saved_upload_and_gcs_reference_count_as_one_attachment():
    service = Artifacts()
    part = types.Part(file_data=types.FileData(file_uri="gs://bucket/paper.pdf", mime_type="application/pdf", display_name="paper.pdf"))
    selected = choose(context('[Uploaded Artifact: "paper.pdf"]', parts=[part], artifacts=service))
    assert selected.upload == b"%PDF-1.7 saved paper"
    assert len(service.calls) == 1


def test_artifact_uri_is_loaded_with_scope_checks():
    service = Artifacts()
    uri = "artifact://apps/replicagent/users/user-a/sessions/session-a/artifacts/paper.pdf/versions/2"
    part = types.Part(file_data=types.FileData(file_uri=uri, mime_type="application/pdf"))
    assert choose(context(parts=[part], artifacts=service)).upload == b"%PDF-1.7 saved paper"
    assert service.calls[0]["version"] == 2
    wrong_scope = types.Part(file_data=types.FileData(file_uri=uri.replace("session-a", "session-b"), mime_type="application/pdf"))
    with pytest.raises(PaperInputError, match="does not belong"):
        choose(context(parts=[wrong_scope], artifacts=service))
    assert len(service.calls) == 1


def test_unsupported_attachment_is_reported_instead_of_falling_back():
    part = types.Part(inline_data=types.Blob(mime_type="image/png", data=b"image"))
    with pytest.raises(PaperInputError, match="PDF"):
        choose(context(parts=[part], state={"requested_paper_source": "old.tex"}))

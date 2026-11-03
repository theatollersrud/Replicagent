"""Select a paper from the current request without substituting a demo paper."""

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urlparse, urlunparse

from google.adk.artifacts import artifact_util

from .services import normalize_artifact_url


class PaperInputError(ValueError):
    """The current message does not identify one readable paper."""


@dataclass(frozen=True)
class PaperSelection:
    source: str
    upload: bytes | None = None
    repository_url: str | None = None
    requested_audit_id: str | None = None


_URL = re.compile(r"(?:https?://|(?:www\.)?arxiv\.org/)[^\s<>\"']+", re.I)
_ARXIV_ID = re.compile(r"(?<![\w./])(?:arxiv:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?)(?!\w|\.\d)", re.I)
_PLACEHOLDER = re.compile(r'\[Uploaded Artifact: "([^"]+)"\]|Uploaded file: (.+?)\. It is saved into artifacts')
_HELP = "I need a paper URL, an arXiv identifier, or a PDF attachment before I can start an audit."


def _url(value: str) -> str:
    value = value.strip().rstrip(".,;!)]}")
    if value.lower().startswith(("arxiv.org/", "www.arxiv.org/")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() == "http":
        parsed = parsed._replace(scheme="https")
    elif parsed.scheme.lower() == "https":
        parsed = parsed._replace(scheme="https")
    if parsed.hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        match = re.fullmatch(r"/(?:abs|pdf|html|e-print|src)/(\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?/?", parsed.path, re.I)
        if match:
            return f"https://arxiv.org/abs/{match[1]}"
    return urlunparse(parsed)


def _is_pdf(data) -> bool:
    mime = (getattr(data, "mime_type", None) or "").split(";", 1)[0].lower()
    name = getattr(data, "display_name", None) or ""
    uri = getattr(data, "file_uri", None) or ""
    payload = getattr(data, "data", None)
    return (
        mime == "application/pdf"
        or name.lower().endswith(".pdf")
        or urlparse(uri).path.lower().endswith(".pdf")
        or isinstance(payload, bytes) and payload.lstrip().startswith(b"%PDF")
    )


def _requested_source(value: str) -> str:
    identifier = _ARXIV_ID.fullmatch(value)
    if identifier:
        return f"https://arxiv.org/abs/{identifier[1]}"
    return _url(value) if _URL.fullmatch(value) else value


async def select_paper(ctx) -> PaperSelection:
    parts = getattr(getattr(ctx, "user_content", None), "parts", None) or []
    text = " ".join(part.text for part in parts if getattr(part, "text", None)).strip()
    state = getattr(ctx.session, "state", {}) or {}
    requested = state.get("requested_paper_source")
    protocol = isinstance(requested, str) and bool(requested.strip()) and text == f"Audit {requested}"
    app_name = getattr(ctx, "app_name", None) or getattr(ctx.session, "app_name", "replicagent")
    user_id = getattr(ctx, "user_id", None) or getattr(ctx.session, "user_id", None)
    session_id = ctx.session.id
    candidates: dict[str, tuple[str, bytes | None]] = {}
    repositories = []
    loaded_names = set()
    attachment_seen = False

    def add(source: str, upload: bytes | None = None):
        key = "bytes:" + hashlib.sha256(upload).hexdigest() if upload is not None else source
        candidates[key] = (source, upload)

    async def load_artifact(filename: str, version: int | None = None):
        service = getattr(ctx, "artifact_service", None)
        if service is None:
            raise PaperInputError("The uploaded file is unavailable in this session. Please attach the PDF again or provide its URL.")
        try:
            artifact = await service.load_artifact(
                app_name=app_name, user_id=user_id, session_id=session_id,
                filename=filename, version=version,
            )
        except Exception as exc:
            raise PaperInputError("The uploaded file could not be read. Please attach the PDF again or provide its URL.") from exc
        if artifact is None:
            raise PaperInputError("The uploaded file is unavailable in this session. Please attach the PDF again or provide its URL.")
        loaded_names.add(filename)
        await attachment(artifact, depth=1)

    async def attachment(part, depth: int = 0):
        if depth > 5:
            raise PaperInputError("The uploaded file reference could not be resolved. Please attach the PDF again.")
        blob = getattr(part, "inline_data", None)
        if blob is not None:
            if _is_pdf(blob):
                if not blob.data:
                    raise PaperInputError("The supplied PDF is empty. Please attach the PDF again.")
                add(blob.display_name or "uploaded-paper.pdf", blob.data)
            return
        file_data = getattr(part, "file_data", None)
        if file_data is None:
            return
        uri = file_data.file_uri or ""
        if uri.startswith("artifact://"):
            try:
                reference = artifact_util.resolve_artifact_reference(
                    file_uri=uri, app_name=app_name, user_id=user_id,
                    session_id=session_id, remaining_depth=5 - depth,
                )
            except Exception as exc:
                raise PaperInputError("The uploaded file reference does not belong to this session or is invalid.") from exc
            if reference.filename not in loaded_names:
                await load_artifact(reference.filename, reference.version)
            return
        if _is_pdf(file_data):
            if uri.lower().startswith(("https://", "http://")):
                add(_url(uri))
            elif uri.startswith("gs://") and urlparse(uri).netloc and urlparse(uri).path.strip("/"):
                add(uri)
            else:
                raise PaperInputError("This PDF attachment uses an unsupported file reference. Please upload the PDF again or provide its public HTTPS URL.")

    # Saved-upload placeholders and their file_data references describe the same
    # attachment. Load the session artifact once before inspecting URI parts.
    for match in _PLACEHOLDER.finditer(text):
        attachment_seen = True
        await load_artifact(match[1] or match[2])
    for part in parts:
        blob = getattr(part, "inline_data", None)
        file_data = getattr(part, "file_data", None)
        if blob is not None or file_data is not None:
            attachment_seen = True
            if file_data is not None and file_data.display_name in loaded_names:
                continue
            await attachment(part)

    request_text = _PLACEHOLDER.sub(" ", text)
    for match in _URL.finditer(request_text):
        source = _url(match[0])
        repository = normalize_artifact_url(source)
        if repository:
            if repository not in repositories:
                repositories.append(repository)
        else:
            add(source)
    remaining_text = _URL.sub(" ", request_text)
    for match in _ARXIV_ID.finditer(remaining_text):
        add(f"https://arxiv.org/abs/{match[1]}")
    if not candidates and protocol:
        add(_requested_source(requested))
    if len(candidates) > 1:
        raise PaperInputError("I found more than one paper in this message. Please send one paper at a time.")
    if not candidates:
        if attachment_seen:
            raise PaperInputError("I could not identify a PDF in the supplied attachments. Please upload one PDF or provide the paper's URL.")
        raise PaperInputError(_HELP)

    source, upload = next(iter(candidates.values()))
    # A runner that sends a PDF also records its hash, so request metadata applies only to that file.
    requested_upload = state.get("requested_upload_sha256")
    same_upload = upload is None if not requested_upload else upload is not None and hashlib.sha256(upload).hexdigest() == requested_upload
    matches_request = protocol and same_upload and source == _requested_source(requested)
    repository = repositories[0] if repositories else state.get("requested_repository_url") if matches_request else None
    audit_id = state.get("requested_audit_id") if matches_request else None
    return PaperSelection(source, upload, repository, audit_id)

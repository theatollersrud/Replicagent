"""Bounded ingestion and static inspection. Repository code is never executed."""

import ast
import hashlib
import io
import json
import re
import socket
import ipaddress
import zipfile
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, quote

import httpx
from pypdf import PdfReader

from .storage import EvidenceLedger, ROOT, audit_dir, save

MAX_DOWNLOAD = 320 * 1024 * 1024
USER_AGENT = "Replicagent-local-audit-demo/0.1"


def public_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS URLs are supported.")
    for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("Local and private network URLs are blocked.")


def download(url: str, target: Path, limit: int = MAX_DOWNLOAD):
    if target.exists() and target.stat().st_size > 0:
        return
    current = url
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(6):
            public_url(current)
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    current = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError(f"The source returned HTTP {response.status_code} instead of a file.")
                total = 0
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=target.parent)
                import os
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    with temporary.open("wb") as stream:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise ValueError("The source exceeds the demo download limit.")
                            stream.write(chunk)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                return
    raise ValueError("The source redirected too many times.")


def get_json(url: str, params: dict | None = None):
    public_url(url)
    response = httpx.get(url, params=params, timeout=35, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.json()


def paper_url(source: str):
    match = re.search(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5}(?:v\d+)?)", source)
    return f"https://arxiv.org/pdf/{match[1]}" if match else source


def ingest_paper(audit_id: str, source: str, upload: bytes | None = None):
    ledger = EvidenceLedger(audit_id)
    if upload:
        path = audit_dir(audit_id) / "paper.pdf"
        path.write_bytes(upload)
        source = "uploaded-paper.pdf"
    elif source.startswith("https://"):
        url = paper_url(source)
        key = hashlib.sha256(url.encode()).hexdigest()[:20]
        path = ROOT / "inputs" / f"{key}.pdf"
        download(url, path, 25 * 1024 * 1024)
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_relative_to(ROOT.resolve()):
            raise ValueError("Copy local input files into the project's inputs folder first.")
    if path.suffix.lower() == ".tex":
        pages = [path.read_text(encoding="utf-8")]
        links = re.findall(r"https://[^\s{}<>]+", pages[0])
    else:
        reader = PdfReader(path)
        if len(reader.pages) > 80:
            raise ValueError("The demo supports papers of up to 80 pages.")
        pages = [page.extract_text() or "" for page in reader.pages]
        links = []
        for page in reader.pages:
            for annotation in page.get("/Annots", []):
                uri = annotation.get_object().get("/A", {}).get("/URI", "")
                if uri.startswith("https://"):
                    links.append(uri)
        links.extend(re.findall(r"https://[^\s<>]+", "\n".join(pages)))
    if sum(len(page.strip()) for page in pages) < 200:
        raise ValueError("The paper has insufficient extractable text. The demo does not perform OCR.")
    records = []
    text_budget = 180_000
    for index, text in enumerate(pages, 1):
        record = ledger.add("paper", source, f"page {index}", text, "PDF text extraction" if path.suffix.lower() == ".pdf" else "LaTeX source read")
        if text_budget > 0:
            excerpt = text[:text_budget]
            records.append({"evidence_id": record.evidence_id, "location": record.location, "text": excerpt})
            text_budget -= len(excerpt)
    document = {
        "source": source, "local_path": str(path), "pages": len(pages),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "links": sorted(set(links)), "evidence": records,
        "text_truncated": sum(map(len, pages)) > 180_000,
    }
    save(audit_id, "paper.json", document)
    return document


def discover_zenodo(title: str, source: str):
    words = [w for w in re.findall(r"[A-Za-z]{4,}", title) if w.lower() not in {"with", "from", "paper", "research", "generation", "large", "language", "models"}]
    query = " AND ".join(f"title:{w}" for w in words[:3])
    if not query:
        return None
    response = get_json("https://zenodo.org/api/records", {"q": query, "size": 5})
    identifier = re.search(r"\d{4}\.\d{4,5}", source)
    for hit in response.get("hits", {}).get("hits", []):
        metadata = hit.get("metadata", {})
        # A title match alone is insufficient. Require a matching paper identifier.
        if identifier and identifier[0] in json.dumps(metadata):
            return f"https://zenodo.org/records/{hit['id']}"
    return None


def get_archive(url: str):
    parsed = urlparse(url)
    if parsed.hostname in {"github.com", "www.github.com"}:
        segments = parsed.path.strip("/").split("/")
        if len(segments) < 2:
            raise ValueError("The GitHub URL must identify a repository.")
        owner, repo = segments[:2]
        repo = repo.removesuffix(".git")
        base = f"https://api.github.com/repos/{owner}/{repo}"
        revision = get_json(base + "/commits", {"per_page": 1})[0]["sha"]
        return f"https://codeload.github.com/{owner}/{repo}/zip/{revision}", revision, {}
    if parsed.hostname == "zenodo.org":
        match = re.search(r"/(?:records|record)/(\d+)", parsed.path)
        if not match:
            raise ValueError("The Zenodo URL must identify a record.")
        metadata = get_json(f"https://zenodo.org/api/records/{match[1]}")
        archives = [f for f in metadata.get("files", []) if f["key"].lower().endswith(".zip")]
        if not archives:
            raise ValueError("This Zenodo record has no supported ZIP archive.")
        artifact = min(archives, key=lambda f: f["size"])
        if artifact["size"] > MAX_DOWNLOAD:
            raise ValueError("The replication package exceeds the 320 MiB demo limit.")
        return artifact["links"]["self"], f"zenodo:{match[1]}", metadata
    raise ValueError("Static package inspection currently supports public GitHub and Zenodo ZIP archives.")


def inspect_archive(audit_id: str, archive_path: Path, source: str, revision: str):
    ledger = EvidenceLedger(audit_id)
    files = {}
    syntax_errors = []
    with zipfile.ZipFile(archive_path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) > 50_000:
            raise ValueError("The archive exceeds the bounded static-inspection limits.")
        for member in members:
            name = PurePosixPath(member.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in member.filename:
                raise ValueError("The archive contains an unsafe file path.")
        paths = [m.filename for m in members]
        index = ledger.add("archive_index", source, revision, "\n".join(paths), "Read the complete ZIP directory without extracting or executing files.")
        candidates = [m for m in members if PurePosixPath(m.filename).suffix.lower() in {".py", ".sh", ".r", ".jl", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".ipynb", ".csv", ".lock"} and m.file_size < 1_500_000]
        def priority(member):
            name = member.filename.lower()
            return (0 if "readme" in name else 1 if any(x in name for x in ("requirements", "environment", "pyproject", "scripts/", "analysis/")) else 2, member.file_size)
        bytes_read = 0
        for member in sorted(candidates, key=priority)[:180]:
            if bytes_read + member.file_size > 20_000_000:
                continue
            files[member.filename] = archive.read(member).decode("utf-8", errors="replace")
            bytes_read += member.file_size
    implementation = [p for p in paths if PurePosixPath(p).suffix.lower() in {".py", ".ipynb", ".r", ".jl", ".java", ".cpp", ".c"}]
    source_files = []
    budget = 100_000
    for name, text in files.items():
        if name.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError as exc:
                source_line = (exc.text or "").strip()
                observation = f"Python {sys.version_info.major}.{sys.version_info.minor} ast.parse reported {exc.msg} at {name}:{exc.lineno}.\nSource: {source_line}"
                diagnostic = ledger.add("static_analysis", source, f"{revision}, {name}, line {exc.lineno}", observation, "Parsed the source with Python AST without importing or executing it.")
                syntax_errors.append({"file": name, "line": exc.lineno, "message": exc.msg, "evidence_id": diagnostic.evidence_id, "quote": observation})
        if name.endswith(".ipynb"):
            try:
                notebook = json.loads(text)
                text = "\n\n".join(f"Cell {i} ({cell['cell_type']})\n" + "".join(cell.get("source", [])) for i, cell in enumerate(notebook.get("cells", []), 1))
            except (ValueError, KeyError):
                continue
        if budget <= 0 or len(source_files) >= 30:
            continue
        excerpt = text[:min(12_000, budget)]
        if not excerpt.strip():
            continue
        location = f"{revision}, {name}, " + ("normalized notebook cells" if name.endswith(".ipynb") else f"lines 1-{len(excerpt.splitlines())}")
        record = ledger.add("repository_file", source, location, excerpt, "Read source text from the pinned ZIP archive. The excerpt may be truncated.")
        source_files.append({"file": name, "evidence_id": record.evidence_id, "location": location, "text": excerpt})
        budget -= len(excerpt)
    readmes = "\n".join(t for p, t in files.items() if "readme" in PurePosixPath(p).name.lower())
    manifests = [p for p in paths if PurePosixPath(p).name.lower() in {"pyproject.toml", "environment.yml", "environment.yaml", "pipfile", "poetry.lock", "uv.lock", "setup.py"} or re.fullmatch(r"requirements[^/]*\.txt", PurePosixPath(p).name.lower())]
    manifest_text = "\n".join(files.get(p, "") for p in manifests)
    facts = {
        "implementation": bool(implementation),
        "stored_results": any(PurePosixPath(p).suffix.lower() in {".csv", ".parquet", ".pkl", ".npy", ".npz"} for p in paths),
        "readme": bool(readmes.strip()),
        "installation_command": bool(re.search(r"(?:pip\s+install|conda\s+(?:env\s+create|install)|uv\s+sync|poetry\s+install)", readmes, re.I)),
        "evaluation_command": bool(re.search(r"(?:python\S*\s+[^\n]*(?:eval|test|analys)|jupyter\s+[^\n]+|\./scripts/[^\n]+)", readmes, re.I)),
        "experiment_configuration": any(PurePosixPath(p).suffix.lower() in {".yaml", ".yml", ".json"} and any(x in p.lower() for x in ("config", "param")) for p in paths) or bool(re.search(r"--seed\b", readmes) and re.search(r"--(?:num_outputs|max_new_tokens|learning.rate|batch.size)\b", readmes)),
        "dependency_manifest": bool(manifests),
        "version_constraints": bool(re.search(r"[=<>~]{1,2}\s*\d+\.\d+", manifest_text)),
    }
    checks = ledger.add("static_checks", source, revision, json.dumps(facts, sort_keys=True), "Applied deterministic file-presence and documentation-pattern checks to the inspected package.")
    return {
        "source": source, "revision": revision, "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "status": "inspected", "file_count": len(paths), "implementation_files": len(implementation),
        "files_read": len(files), "file_index_evidence_id": index.evidence_id,
        "file_paths": paths, "source_excerpts": source_files, "syntax_errors": syntax_errors,
        "facts": facts, "static_checks_evidence_id": checks.evidence_id, "local_archive_path": str(archive_path),
        "limitations": ["Static inspection sampled up to 180 small text files, 20 MB of source text, and 30 source excerpts. Files over 1.5 MB were indexed but skipped. Presence checks are a coarse demo rubric.", "Stored result files were detected by extension. Their completeness and scientific validity remain unverified."],
    }


def inspect_repository(audit_id: str, manifest: dict, document: dict, repository_url: str | None = None):
    attempts = []
    source = repository_url
    if not source:
        links = document["links"] + manifest.get("artifact_urls", [])
        supported = [u for u in links if urlparse(u).hostname in {"github.com", "zenodo.org"}]
        source = supported[0] if supported else None
    if not source:
        try:
            source = discover_zenodo(manifest["title"], document["source"])
            if source:
                attempts.append("A Zenodo title search found metadata containing the exact arXiv identifier.")
        except Exception as exc:
            attempts.append(f"The Zenodo discovery request was blocked: {type(exc).__name__}: {str(exc)[:200]}")
    if not source:
        return {"status": "blocked", "source": None, "revision": None, "facts": {}, "source_excerpts": [], "file_paths": [], "limitations": ["No supported package was located. Code availability remains unknown.", *attempts]}
    try:
        url, revision, metadata = get_archive(source)
        key = hashlib.sha256(url.encode()).hexdigest()[:20]
        path = ROOT / "inputs" / f"{key}.zip"
        download(url, path)
        result = inspect_archive(audit_id, path, source, revision)
        if metadata:
            EvidenceLedger(audit_id).add("artifact_metadata", source, revision, json.dumps(metadata.get("metadata", {}), ensure_ascii=False), "Read the public Zenodo API metadata.")
        result["discovery"] = attempts
        return result
    except Exception as exc:
        return {"status": "blocked", "source": source, "revision": None, "facts": {}, "source_excerpts": [], "file_paths": [], "limitations": [f"Package inspection was blocked: {type(exc).__name__}: {str(exc)[:300]}", *attempts]}

"""Bounded reference verification. Third-party lookups stop at the first failure or at the time budget."""

import difflib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

import httpx

from .storage import EvidenceLedger

USER_AGENT = "Replicagent-local-audit-demo/0.1"
BUDGET_SECONDS = 60
REQUEST_TIMEOUT = 6
WORKERS = 2
MAX_REFERENCES = 60
TITLE_MATCH = 0.75


def _braces(text: str, start: int) -> str:
    """Return the content of the brace group starting at text[start] == '{'."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    return text[start + 1:]


def _field(block: str, name: str) -> str | None:
    match = re.search(r"\\bibinfo\{" + name + r"\}\s*\{", block)
    return _field_clean(_braces(block, match.end() - 1)) if match else None


def _field_clean(value: str) -> str:
    value = re.sub(r"\\(?:emph|textit|textbf|mbox|natexlab|nolinkurl)\s*\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\[A-Za-z]+\*?", " ", value)
    value = re.sub(r"[{}~]", " ", value)
    return " ".join(value.split())


def parse_bibliography(text: str) -> list[dict]:
    """Parse \\bibitem entries from a .bbl or thebibliography environment."""
    blocks = re.split(r"(?=\\bibitem)", text)
    references = []
    for block in blocks:
        header = re.match(r"\\bibitem(?:\[(?:[^\[\]]|\[[^\]]*\])*\])?\s*%?\s*\{([^}]*)\}", block, re.S)
        if not header:
            continue
        key = header.group(1).strip()
        body = block[header.end():].split("\\end{thebibliography}")[0]
        title = _field(body, "title")
        if title is None:
            shown = re.search(r"\\showarticletitle\s*\{", body)
            if shown:
                title = _field_clean(_braces(body, shown.end() - 1))
        if title is None:
            blocks_ = [b.strip() for b in re.split(r"\\newblock", body) if b.strip()]
            title = _field_clean(blocks_[1]) if len(blocks_) > 1 else _field_clean(body)
            title = re.split(r"\.\s+In\b|\.\s*$", title, maxsplit=1)[0]
        doi = re.search(r"doi\.org/(10\.\d{4,9}/[^\s}\"<>]+)", body, re.I) or re.search(r"\bdoi:\s*\\?(?:nolinkurl\{)?(10\.\d{4,9}/[^\s}\"<>]+)", body, re.I)
        arxiv = re.search(r"\\showeprint\[arxiv\]\{([^}]+)\}", body, re.I) or re.search(r"arXiv[:\s]+(\d{4}\.\d{4,5})", body, re.I) or re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", body, re.I)
        year = _field(body, "year") or (re.search(r"\b(19|20)\d{2}\b", body) or [None])[0]
        references.append({
            "key": key, "title": title[:300], "year": str(year)[:4] if year else None,
            "doi": doi.group(1).rstrip(".") if doi else None, "arxiv": arxiv.group(1).strip() if arxiv else None,
            "authors": (_field(body, "person") or "")[:120],
        })
    return references[:MAX_REFERENCES]


def citation_contexts(text: str, keys: set[str], window: int = 240) -> dict[str, list[str]]:
    """Return the sentence fragments around each \\cite of the given keys."""
    contexts: dict[str, list[str]] = {}
    for match in re.finditer(r"\\cite[tp]?\*?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", text):
        cited = [k.strip() for k in match.group(1).split(",")]
        start = max(0, match.start() - window)
        fragment = " ".join(text[start:match.end() + 80].split())
        for key in cited:
            if key in keys and len(contexts.setdefault(key, [])) < 2:
                contexts[key].append(fragment)
    return contexts


def _similar(left: str, right: str) -> float:
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.casefold())
    return difflib.SequenceMatcher(None, norm(left), norm(right)).ratio()


def _service(reference: dict) -> str:
    doi = (reference.get("doi") or "").casefold()
    if doi and not doi.startswith("10.48550/arxiv."):
        return "crossref"
    return "arxiv" if reference.get("arxiv") or doi.startswith("10.48550/arxiv.") else "crossref"


def _lookup(reference: dict, client: httpx.Client) -> dict:
    """Resolve one reference. Raises on transport or server failures so the caller can abort."""
    doi = reference.get("doi") or ""
    if doi.casefold().startswith("10.48550/arxiv.") and not reference.get("arxiv"):
        reference = {**reference, "arxiv": doi.split("arXiv.", 1)[-1]}
    if doi and not doi.casefold().startswith("10.48550/arxiv."):
        response = client.get(f"https://api.crossref.org/works/{doi}")
        if response.status_code == 404:
            return {"status": "not_found", "detail": "The DOI does not resolve on Crossref.", "service": "crossref"}
        response.raise_for_status()
        titles = response.json().get("message", {}).get("title", [])
        found = titles[0] if titles else ""
        score = _similar(reference["title"], found)
        return {"status": "found" if score >= TITLE_MATCH else "title_mismatch", "matched_title": found, "similarity": round(score, 2), "service": "crossref"}
    if reference.get("arxiv"):
        response = client.get("https://export.arxiv.org/api/query", params={"id_list": reference["arxiv"], "max_results": 1})
        response.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = ElementTree.fromstring(response.text).findall("a:entry", ns)
        found = " ".join((entries[0].findtext("a:title", default="", namespaces=ns) or "").split()) if entries else ""
        if not found or found.casefold().startswith("error"):
            return {"status": "not_found", "detail": "arXiv has no entry for this identifier.", "service": "arxiv"}
        score = _similar(reference["title"], found)
        return {"status": "found" if score >= TITLE_MATCH else "title_mismatch", "matched_title": found, "similarity": round(score, 2), "service": "arxiv"}
    if len(reference.get("title") or "") < 12:
        return {"status": "unchecked", "detail": "The entry has no usable title, DOI, or arXiv identifier.", "service": None}
    response = client.get("https://api.crossref.org/works", params={"query.bibliographic": reference["title"], "rows": 1})
    response.raise_for_status()
    items = response.json().get("message", {}).get("items", [])
    found = (items[0].get("title") or [""])[0] if items else ""
    score = _similar(reference["title"], found)
    return {"status": "found" if score >= TITLE_MATCH else "not_found", "matched_title": found, "similarity": round(score, 2), "service": "crossref"}


def verify_references(audit_id: str, source: str, paper_text: str, budget: float = BUDGET_SECONDS) -> dict:
    """Check that references exist. Stops at the first lookup failure instead of retrying."""
    references = parse_bibliography(paper_text)
    if not references:
        return {"status": "not_attempted", "complete": False, "reason": "No parseable bibliography entries were found in the paper source.", "references": [], "counts": {}}
    deadline = time.monotonic() + budget
    # A failure stops further lookups on that service only. arXiv entries are checked first because they use a different service.
    abort = {"crossref": threading.Event(), "arxiv": threading.Event()}
    abort_reason: dict[str, str] = {}
    results = [dict(reference, status="unchecked", detail="Not reached before the lookup budget was spent.") for reference in references]
    order = sorted(range(len(references)), key=lambda i: 0 if _service(references[i]) == "arxiv" else 1)

    def check(index: int, client: httpx.Client):
        service = _service(references[index])
        if abort[service].is_set() or time.monotonic() > deadline:
            results[index]["detail"] = f"Skipped after an earlier {service} lookup failed." if abort[service].is_set() else results[index]["detail"]
            return
        try:
            time.sleep(0.5 * (index % WORKERS))
            outcome = _lookup(references[index], client)
            results[index].pop("detail", None)
            results[index].update(outcome)
        except Exception as exc:
            abort_reason.setdefault(service, f"{type(exc).__name__}: {str(exc)[:160]}")
            abort[service].set()
            results[index].update(status="unchecked", detail=f"The {service} lookup failed and further {service} lookups were stopped.")

    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(lambda i: check(i, client), order))
    counts = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    failed_services = [name for name, event in abort.items() if event.is_set()]
    complete = counts.get("unchecked", 0) == 0 and not failed_services
    if failed_services:
        reason = "; ".join(f"a {name} lookup failed ({abort_reason.get(name, 'unknown error')})" for name in failed_services)
        reason = reason[0].upper() + reason[1:] + ". Remaining lookups on that service were skipped instead of retried."
    elif time.monotonic() > deadline:
        reason = f"The {budget:g} second reference budget was spent before every entry was checked."
    else:
        reason = None
    observation = json.dumps({"counts": counts, "complete": complete, "reason": reason, "references": results}, ensure_ascii=False, sort_keys=True)
    record = EvidenceLedger(audit_id).add(
        "reference_check", source, "bibliography", observation,
        "Resolved DOIs on Crossref, arXiv identifiers on the arXiv API, and otherwise searched Crossref by title; compared titles by string similarity. No retries.",
    )
    return {
        "status": "verified" if complete and not counts.get("not_found") and not counts.get("title_mismatch") else "partially_verified" if counts.get("found") else "failed" if complete else "blocked",
        "complete": complete, "reason": reason, "counts": counts, "references": results, "evidence_id": record.evidence_id, "failed_services": failed_services,
        "checked": len(references) - counts.get("unchecked", 0), "total": len(references),
    }

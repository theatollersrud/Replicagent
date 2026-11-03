"""Load a useful slice of audit evidence without duplicating entire repositories."""
import json
from pathlib import PurePosixPath

MAX_CONTEXT_BYTES = 200_000
FULL_RECORD_PATH = "/workspace/.replicagent-audit/context.json"


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore") + "\n[Excerpt shortened. Read the full record or repository file if needed.]"


def _bounded(value, limit):
    """Keep normal structured records intact, and explicitly excerpt large ones."""
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= limit:
        return value
    return {"excerpt": _clip(encoded.decode("utf-8"), limit), "full_record_available": True}


def _priority(path):
    name = PurePosixPath(path).name.lower()
    parts = PurePosixPath(path).parts
    source = PurePosixPath(path).suffix.lower() in {
        ".py", ".sh", ".r", ".jl", ".c", ".cpp", ".java", ".js", ".ts", ".ipynb",
    }
    metadata = name.startswith(("readme", "requirements")) or name in {
        "pyproject.toml", "setup.py", "setup.cfg", "package.json", "makefile",
        "environment.yml", "environment.yaml", "cargo.toml", "cmakelists.txt",
    }
    tests = any(p.lower() in {"test", "tests", "examples", "scripts"} for p in parts)
    return (0 if metadata else 1 if tests else 2 if source else 3, len(parts), len(path), path)


def _paths(paths, count, bytes_available):
    selected = []
    used = 0
    for path in sorted((p for p in paths if isinstance(p, str)), key=_priority):
        size = len(json.dumps(path, ensure_ascii=False).encode("utf-8"))
        if len(selected) >= count or used + size > bytes_available:
            continue
        selected.append(path)
        used += size
    return selected


def _recent_attempt(attempt, output_bytes):
    result = {key: attempt.get(key) for key in (
        "argv", "working_directory", "purpose", "kind", "exit_code",
        "elapsed_seconds", "timed_out",
    )}
    result = {key: _bounded(value, 2500) for key, value in result.items()}
    # Preserve the end of tracebacks and command summaries, not just log headers.
    output = str(attempt.get("stdout_tail", "")).encode("utf-8")
    result["stdout_tail"] = output[-output_bytes:].decode("utf-8", errors="ignore")
    result["output_excerpted"] = len(output) > output_bytes or attempt.get("output_truncated", False)
    return result


def planner_contents(context, attempts, remaining_seconds, extraction, allow_egress=False, compact=False):
    """Select input evidence only. This never changes generation/reasoning settings."""
    repository = context.get("repository", {})
    if not isinstance(repository, dict):
        repository = {}
    file_paths = repository.get("file_paths", [])
    sample = _paths(file_paths, 40 if compact else 180, 6000 if compact else 24_000)
    excerpts = repository.get("source_excerpts", [])
    if isinstance(excerpts, list):
        excerpts = sorted(
            [entry for entry in excerpts if isinstance(entry, dict)],
            key=lambda entry: _priority(str(entry.get("file") or entry.get("path") or entry.get("location") or "")),
        )
    else:
        excerpts = []
    excerpt_budget = 4000 if compact else 40_000
    chosen_excerpts = []
    for excerpt in excerpts:
        selection = _bounded(excerpt, min(3500, excerpt_budget))
        size = len(json.dumps(selection, ensure_ascii=False).encode("utf-8"))
        if size > excerpt_budget:
            continue
        chosen_excerpts.append(selection)
        excerpt_budget -= size
        if excerpt_budget < 500:
            break
    recent_count = 2 if compact else 6
    older = attempts[:-recent_count] if len(attempts) > recent_count else []
    earlier_summaries = [{
        "attempt": index + 1,
        "kind": _clip(a.get("kind", ""), 40),
        "command": _clip(a.get("argv", []), 350),
        "exit_code": a.get("exit_code"),
        "timed_out": a.get("timed_out", False),
    } for index, a in enumerate(older)]
    # Older observations remain in the on-disk record even in long command loops.
    earlier_summaries = earlier_summaries[-(8 if compact else 30):]
    skipped = extraction.get("skipped", [])
    payload = {
        "remaining_seconds": round(remaining_seconds, 1),
        "environment": {"outbound_internet": allow_egress, "cpu_only": True},
        "paper_and_repository": {
            "manifest": _bounded(context.get("manifest", {}), 3000 if compact else 12_000),
            "review": _bounded(context.get("review", {}), 2000 if compact else 10_000),
            "repository": {
                "source": _clip(repository.get("source", ""), 1000),
                "revision": _clip(repository.get("revision", ""), 200),
                "inventory": {"total_files": len(file_paths), "sample_paths": sample,
                              "omitted_files": len(file_paths) - len(sample)},
                "source_excerpts": chosen_excerpts,
            },
        },
        "extraction": {
            "total_files_extracted": len(extraction.get("files", [])),
            "bytes": extraction.get("bytes"),
            "skipped_count": len(skipped),
            "skipped_sample": [_bounded(item, 500) for item in skipped[:(3 if compact else 12)]],
        },
        "previous_attempts": {
            "total_attempts": len(attempts),
            "older": earlier_summaries,
            "older_attempts_not_shown": len(older) - len(earlier_summaries),
            "recent": [_recent_attempt(a, 1500 if compact else 10_000) for a in attempts[-recent_count:]],
        },
        "context_loading": {
            "full_record_path": FULL_RECORD_PATH,
            "selection_note": "This is selected evidence, not the complete repository. Counts describe the full inventory. Inspect relevant files in /workspace with targeted commands. The full audit context and earlier command observations are in the record at full_record_path.",
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        # JSON escaping can expand control-heavy data beyond field-level estimates.
        if not compact:
            return planner_contents(context, attempts, remaining_seconds, extraction, allow_egress, compact=True)
        payload["paper_and_repository"]["manifest"] = {"title": _clip(
            context.get("manifest", {}).get("title", "") if isinstance(context.get("manifest"), dict) else "", 500)}
        payload["paper_and_repository"]["review"] = {}
        payload["paper_and_repository"]["repository"]["source_excerpts"] = []
        payload["previous_attempts"]["recent"] = [_recent_attempt(a, 300) for a in attempts[-1:]]
        payload["previous_attempts"]["older"] = []
        encoded = json.dumps(payload, ensure_ascii=False)
    assert len(encoded.encode("utf-8")) <= MAX_CONTEXT_BYTES, "The selected input exceeds the provider-safe request size."
    return encoded

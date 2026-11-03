"""arXiv LaTeX source retrieval and LaTeX-aware quote matching."""

import gzip
import io
import re
import tarfile
from pathlib import PurePosixPath

LATEX_METHOD = "LaTeX source read"
MAX_SOURCE_CHARS = 2_000_000
MAX_INCLUDE_DEPTH = 5


def arxiv_identifier(source: str) -> str | None:
    match = re.search(r"arxiv\.org/(?:abs|pdf|html|e-print|src)/(\d{4}\.\d{4,5}(?:v\d+)?)", source)
    return match[1] if match else None


def arxiv_source_url(identifier: str) -> str:
    return f"https://arxiv.org/e-print/{identifier}"


def _decompress(payload: bytes) -> bytes:
    if payload[:2] == b"\x1f\x8b":
        return gzip.decompress(payload)
    return payload


def _tex_files(payload: bytes) -> dict[str, str]:
    """Return the .tex members of an arXiv e-print as text, keyed by archive path."""
    files = {}
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError:
        text = payload.decode("utf-8", errors="replace")
        return {"main.tex": text} if "\\documentclass" in text or "\\begin{document}" in text else {}
    with archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("The arXiv source archive contains an unsafe file path.")
            if not member.isfile() or name.suffix.lower() != ".tex" or member.size > MAX_SOURCE_CHARS:
                continue
            stream = archive.extractfile(member)
            if stream is not None:
                files[member.name] = stream.read().decode("utf-8", errors="replace")
    return files


def _main_file(files: dict[str, str]) -> str | None:
    candidates = [name for name, text in files.items() if re.search(r"^[^%\n]*\\documentclass", text, re.M)]
    with_body = [name for name in candidates if "\\begin{document}" in files[name]]
    pool = with_body or candidates
    return max(pool, key=lambda name: len(files[name])) if pool else None


def _resolve(files: dict[str, str], base: str, target: str) -> str | None:
    target = target.strip().strip("{}").strip()
    if not target:
        return None
    options = [target, target + ".tex"]
    parent = PurePosixPath(base).parent
    options += [str(parent / option) for option in options]
    for option in options:
        normalized = str(PurePosixPath(option))
        if normalized in files:
            return normalized
    return None


def flatten(files: dict[str, str], name: str, depth: int = 0, seen: frozenset = frozenset()) -> str:
    """Inline \\input and \\include of sibling files so one document holds every table."""
    text = files[name]
    if depth >= MAX_INCLUDE_DEPTH:
        return text

    def replace(match):
        if match.group(0).lstrip().startswith("%"):
            return match.group(0)
        resolved = _resolve(files, name, match.group(2))
        if resolved is None or resolved in seen:
            return match.group(0)
        return "\n" + flatten(files, resolved, depth + 1, seen | {name}) + "\n"

    return re.sub(r"(?<!\\)\\(input|include)\s*\{([^}]*)\}", replace, text)[:MAX_SOURCE_CHARS]


def extract_latex_source(payload: bytes) -> str | None:
    """Return the flattened LaTeX document from an arXiv e-print, or None when no LaTeX is available."""
    if payload[:4] == b"%PDF":
        return None
    payload = _decompress(payload)
    if payload[:4] == b"%PDF":
        return None
    files = _tex_files(payload)
    main = _main_file(files)
    if main is None:
        return None
    text = flatten(files, main)
    return text if len(text.strip()) >= 200 else None


_FORMATTING = r"\\(?:textbf|textit|textsc|texttt|textrm|textsf|emph|underline|mathbf|mathrm|mathit|mathsf|mathcal|boldsymbol|text|bm|small|footnotesize|scriptsize|tiny|normalsize|large|Large|LARGE|bf|it|tt|sc|rm|sf|centering|raggedright|raggedleft|midrule|toprule|bottomrule|hline|cline|multicolumn|multirow|cmidrule|nonumber|,|;|!|:|quad|qquad|hfill|hspace|vspace|phantom|hphantom|vphantom)\b"


def latex_normalise(text: str) -> str:
    """Reduce LaTeX markup to its visible content so `64$^{(-6.35)}$` and `64^{(-6.35)}` compare equal."""
    text = re.sub(r"(?<!\\)%[^\n]*", "", text)
    text = re.sub(r"\\(?:hphantom|vphantom|phantom|hspace\*?|vspace\*?|label|ref|cite[tp]?\*?|cmidrule|cline)\s*(\[[^\]]*\])?\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\multicolumn\s*\{[^{}]*\}\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\multirow\s*\{[^{}]*\}\s*(\[[^\]]*\])?\s*\{[^{}]*\}", "", text)
    text = re.sub(_FORMATTING + r"\*?", "", text)
    text = re.sub(r"\\(?:to|rightarrow|longrightarrow)\b", "→", text)
    text = re.sub(r"\\\\(?:\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\textbackslash\b", "\\\\", text)
    text = text.replace("~", " ")
    text = re.sub(r"[${}&]", "", text)
    text = re.sub(r"\\(?=[%#_])", "", text)
    return " ".join(text.split()).casefold()

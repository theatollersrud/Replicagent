import gzip
import io
import tarfile

import pytest

from replicagent import services, storage
from replicagent.latex import LATEX_METHOD, arxiv_identifier, extract_latex_source, latex_normalise
from replicagent.storage import EvidenceLedger, verify_quote


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")


def _tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


MAIN = b"\\documentclass{article}\n\\begin{document}\n" + b"Intro text. " * 20 + b"\n\\input{tables/results}\n\\end{document}\n"
TABLE = b"\\begin{tabular}{lr}\nCodeLlama & 59$\\to$\\hphantom{1}64$^{(-6.35)}$ \\\\\nDeepSeek & \\textbf{110}$\\to$\\textbf{122}$^{(+1.02)}$ \\\\\n\\end{tabular}\n"


def test_arxiv_identifier_accepts_abs_pdf_and_versions():
    assert arxiv_identifier("https://arxiv.org/abs/2510.21513") == "2510.21513"
    assert arxiv_identifier("https://arxiv.org/pdf/2510.21513v2") == "2510.21513v2"
    assert arxiv_identifier("https://example.org/paper.pdf") is None


def test_eprint_tarball_is_flattened_from_main_file():
    text = extract_latex_source(_tarball({"main.tex": MAIN, "tables/results.tex": TABLE, "old/draft.tex": b"\\documentclass{article}\nabandoned"}))
    assert "\\documentclass{article}" in text
    assert "CodeLlama & 59$\\to$\\hphantom{1}64$^{(-6.35)}$" in text
    assert "\\input{tables/results}" not in text
    assert "abandoned" not in text


def test_single_gzipped_tex_file_and_pdf_only_eprints():
    assert "Intro text" in extract_latex_source(gzip.compress(MAIN + TABLE))
    assert extract_latex_source(b"%PDF-1.5 binary") is None
    assert extract_latex_source(gzip.compress(b"%PDF-1.5 binary")) is None


def test_unsafe_eprint_paths_are_rejected():
    with pytest.raises(ValueError):
        extract_latex_source(_tarball({"../escape.tex": MAIN}))


def test_latex_markup_is_reduced_to_visible_content():
    row = latex_normalise(r"CodeLlama & 59$\to$\hphantom{1}64$^{(-6.35)}$ & 42$\to$\hphantom{1}47$^{(+0.00)}$ \\")
    assert row == "codellama 59→64^(-6.35) 42→47^(+0.00)"
    assert latex_normalise("64^{(-6.35)}") in row
    assert latex_normalise(r"Defects4J & \hphantom{1}89$\to$112 & 153 & 181 & 205 \\") == "defects4j 89→112 153 181 205"
    assert latex_normalise(r"\textbf{110}$\to$\textbf{122} % comment") == "110→122"


def test_latex_quotes_verify_on_visible_content_only_for_latex_evidence():
    ledger = EvidenceLedger("audit-tex")
    tex = ledger.add("paper", "arxiv", "LaTeX source", TABLE.decode(), LATEX_METHOD)
    pdf = ledger.add("paper", "arxiv", "page 1", TABLE.decode(), "PDF text extraction")
    records = {record.evidence_id: record for record in ledger.records()}
    assert verify_quote(records, tex.evidence_id, "CodeLlama & 59$\\to$\\hphantom{1}64$^{(-6.35)}$")
    assert verify_quote(records, tex.evidence_id, "CodeLlama 59→64^{(-6.35)}")
    assert verify_quote(records, tex.evidence_id, "DeepSeek 110→122")
    assert not verify_quote(records, tex.evidence_id, "CodeLlama 59→65^{(-6.35)}")
    assert not verify_quote(records, tex.evidence_id, "DeepSeek 122→110")
    assert not verify_quote(records, pdf.evidence_id, "CodeLlama 59→64^{(-6.35)}")


@pytest.mark.parametrize("copies", [2, 4])
def test_json_escaped_latex_commands_preserve_quote_checks(copies):
    source = r"DeepSeek & \textbf{110}$\to$\textbf{122}$^{(+1.02)}$ \\"
    ledger = EvidenceLedger("escaped-tex")
    tex = ledger.add("paper", "arxiv", "LaTeX source", source, LATEX_METHOD)
    pdf = ledger.add("paper", "arxiv", "page 1", source, "PDF text extraction")
    records = {record.evidence_id: record for record in ledger.records()}
    quote = source.replace("\\", "\\" * copies)
    assert verify_quote(records, tex.evidence_id, quote)
    assert not verify_quote(records, tex.evidence_id, quote.replace("122", "123"))
    assert not verify_quote(records, tex.evidence_id, quote.replace("110", "999"))
    assert not verify_quote(records, pdf.evidence_id, quote)


def test_arxiv_ingestion_prefers_latex_and_falls_back_to_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "ROOT", tmp_path)
    monkeypatch.setattr(storage, "ROOT", tmp_path)
    payloads = {"https://arxiv.org/e-print/2510.21513": _tarball({"main.tex": MAIN, "tables/results.tex": TABLE})}

    def fake_download(url, target, limit=None):
        if url not in payloads:
            raise AssertionError(f"unexpected download {url}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payloads[url])

    monkeypatch.setattr(services, "download", fake_download)
    document = services.ingest_paper("audit-a", "https://arxiv.org/abs/2510.21513")
    assert document["format"] == "latex" and document["pages"] == 1 and document["ingestion_notes"] == []
    records = {r.evidence_id: r for r in EvidenceLedger("audit-a").records()}
    assert records[document["evidence"][0]["evidence_id"]].verification_method == LATEX_METHOD
    assert "CodeLlama & 59$\\to$" in records[document["evidence"][0]["evidence_id"]].observation

    payloads["https://arxiv.org/e-print/2510.21513"] = b"%PDF-1.5 no source"
    pdf_used = []
    monkeypatch.setattr(services, "PdfReader", lambda path: pdf_used.append(path) or type("R", (), {"pages": [type("P", (), {"extract_text": lambda self: "Rendered paper text. " * 20, "get": lambda self, *a: []})()]})())
    payloads["https://arxiv.org/pdf/2510.21513"] = b"%PDF-1.5 rendered"
    document = services.ingest_paper("audit-b", "https://arxiv.org/abs/2510.21513")
    assert document["format"] == "pdf" and pdf_used
    assert "no LaTeX source" in document["ingestion_notes"][0]

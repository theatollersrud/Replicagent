import json
import sys
import zipfile

import httpx
import pytest

from replicagent import dynamic, references, storage
from replicagent.references import parse_bibliography, verify_references
from replicagent.schemas import Problem
from replicagent.scoring import aggregate_problems, calculate_dynamic_score, dynamic_execution_summary
from replicagent.storage import EvidenceLedger


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "AUDITS", tmp_path / "audits")
    monkeypatch.setattr(dynamic, "ROOT", tmp_path)


def _package(tmp_path):
    archive = tmp_path / "package.zip"
    notebook = {"cells": [
        {"cell_type": "markdown", "source": ["# title"]},
        {"cell_type": "code", "source": ["%matplotlib inline\n", "import csv, json\n", "rows = list(csv.DictReader(open('results.csv')))\n", "open('figure_counts.json', 'w').write(json.dumps({'solved': sum(r['ok'] == 'True' for r in rows)}))\n", "print('done')\n"]},
    ]}
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("pkg/analysis/results.csv", "problem,ok\np1,True\np2,False\np3,True\n")
        zf.writestr("pkg/analysis/make_table.ipynb", json.dumps(notebook))
        zf.writestr("pkg/analysis/plot_broken.py", "import csv\nprint('x'\n")
        zf.writestr("pkg/inference/generate.py", "import torch\nprint('never run')\n")
        zf.writestr("pkg/analysis/needs_module.py", "import definitely_missing_module_xyz\n")
    return archive


def test_compile_check_parses_notebooks_and_reports_failures(tmp_path):
    root = dynamic.extract_archive(_package(tmp_path))
    result = dynamic.compile_check(root)
    assert result["python_files"] == 3 and result["notebooks"] == 1
    assert [f["file"] for f in result["failures"]] == ["analysis/plot_broken.py"]
    assert not result["compiles"]
    assert "torch" in result["third_party_imports"] and "definitely_missing_module_xyz" in result["missing_modules"]


def test_data_inventory_counts_csv_rows(tmp_path):
    root = dynamic.extract_archive(_package(tmp_path))
    inventory = dynamic.data_inventory(root)
    assert inventory["raw_data_present"] and inventory["by_suffix"] == {".csv": 1}
    assert inventory["files"][0]["rows"] == 3 and inventory["files"][0]["columns"] == "problem,ok"


def test_analysis_scripts_exclude_inference_and_heavy_imports(tmp_path):
    root = dynamic.extract_archive(_package(tmp_path))
    found = [str(p.relative_to(root)) for p in dynamic.find_analysis_scripts(root)]
    assert "analysis/make_table.ipynb" in found and "analysis/plot_broken.py" in found
    assert not any("inference" in name for name in found)


def test_notebook_runs_from_its_directory_and_records_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamic, "_install_missing", lambda stderr, root=None: None)
    root = dynamic.extract_archive(_package(tmp_path))
    run = dynamic.run_script(root, root / "analysis" / "make_table.ipynb")
    assert run["succeeded"] and "done" in run["stdout"]
    assert run["produced_files"] == ["analysis/figure_counts.json"]
    assert json.loads((root / "analysis" / "figure_counts.json").read_text())["solved"] == 2
    failed = dynamic.run_script(root, root / "analysis" / "needs_module.py")
    assert not failed["succeeded"] and "No module named" in failed["stderr"]


def test_generated_check_compares_result_line(tmp_path):
    root = dynamic.extract_archive(_package(tmp_path))
    check = {"claim_id": "C01", "description": "count ok rows", "expected_value": "2",
             "script": "import csv\nrows = list(csv.DictReader(open('analysis/results.csv')))\nprint('RESULT=' + str(sum(r['ok'] == 'True' for r in rows)))\n"}
    result = dynamic.run_generated_check(root, check)
    assert result["computed_value"] == "2" and result["matches"] is True
    result = dynamic.run_generated_check(root, {**check, "expected_value": "3"})
    assert result["matches"] is False
    result = dynamic.run_generated_check(root, {**check, "script": "print('no result')"})
    assert result["computed_value"] is None and result["matches"] is None


def test_dynamic_analysis_records_evidence_for_every_run(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamic, "_install_missing", lambda stderr, root=None: None)
    repository = {"status": "inspected", "local_archive_path": str(_package(tmp_path)), "source": "fixture", "revision": "rev"}
    result = dynamic.run_dynamic_analysis("audit-dyn", repository)
    assert result["status"] == "completed"
    assert all(run["evidence_id"] for run in result["runs"])
    types = {record.source_type for record in EvidenceLedger("audit-dyn").records()}
    assert {"dynamic_compile", "data_inventory", "script_execution"} <= types
    assert dynamic.run_dynamic_analysis("audit-none", {"status": "blocked"})["status"] == "not_attempted"


BBL = r"""
\begin{thebibliography}{2}
\bibitem[Ashiga et~al\mbox{.}(2025)]%
        {ashiga2025:ensemble}
\bibfield{author}{\bibinfo{person}{Mari Ashiga}.}
  \bibinfo{year}{2025}\natexlab{}.
\newblock \bibinfo{title}{Ensemble Learning for Large Language Models in Text and Code Generation: A Survey}.
\newblock
\showeprint[arxiv]{2503.13505}~[cs]
\href{https://doi.org/10.48550/arXiv.2503.13505}{doi:\nolinkurl{10.48550/arXiv.2503.13505}}

\bibitem[Chen et~al\mbox{.}(2025)]%
        {chen2025:debatecoder}
\bibfield{author}{\bibinfo{person}{Jizheng Chen}.} \bibinfo{year}{2025}\natexlab{}.
\newblock \showarticletitle{DebateCoder: Towards Collective Intelligence of LLMs}. In \bibinfo{booktitle}{Proceedings of ACL}.

\bibitem{plain2020}
A. Author. \newblock A Title Only Entry About Nothing. \newblock Journal, 2020.
\end{thebibliography}
"""


def test_bibliography_parser_reads_acm_and_plain_entries():
    refs = parse_bibliography(BBL)
    assert [r["key"] for r in refs] == ["ashiga2025:ensemble", "chen2025:debatecoder", "plain2020"]
    assert refs[0]["doi"] == "10.48550/arXiv.2503.13505" and refs[0]["arxiv"] == "2503.13505" and refs[0]["year"] == "2025"
    assert refs[1]["title"].startswith("DebateCoder: Towards Collective Intelligence") and refs[1]["doi"] is None
    assert refs[2]["title"] == "A Title Only Entry About Nothing" and refs[2]["doi"] is None


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


def test_reference_lookups_resolve_and_do_not_retry_after_failure(monkeypatch):
    calls = []

    def fake_get(self, url, params=None):
        calls.append(url)
        if "export.arxiv.org" in url:
            assert params["id_list"] == "2503.13505"
            return _Response(text='<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Ensemble Learning for Large Language Models in\n Text and Code Generation: A Survey</title></entry></feed>')
        if "query.bibliographic" in str(params):
            return _Response(payload={"message": {"items": [{"title": ["Something Unrelated Entirely"]}]}})
        return _Response(status_code=503)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(references, "WORKERS", 1)
    result = verify_references("audit-ref", "fixture", BBL)
    statuses = {r["key"]: r["status"] for r in result["references"]}
    assert statuses["ashiga2025:ensemble"] == "found"
    assert not any("works/10.48550" in url for url in calls), "arXiv DOIs must not be sent to Crossref"
    assert "detail" not in next(r for r in result["references"] if r["key"] == "ashiga2025:ensemble")
    assert statuses["chen2025:debatecoder"] == "not_found"
    assert statuses["plain2020"] == "not_found"
    assert result["complete"] and result["status"] == "partially_verified"

    def crossref_down(self, url, params=None):
        if "export.arxiv.org" in url:
            return _Response(text='<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Ensemble Learning for Large Language Models in Text and Code Generation: A Survey</title></entry></feed>')
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.Client, "get", crossref_down)
    result = verify_references("audit-ref2", "fixture", BBL)
    assert not result["complete"] and "ConnectError" in result["reason"] and result["failed_services"] == ["crossref"]
    assert result["counts"] == {"found": 1, "unchecked": 2} and result["status"] == "partially_verified"
    assert verify_references("audit-ref3", "fixture", "no bibliography here")["status"] == "not_attempted"


def test_dynamic_summary_feeds_the_rubric_and_aggregated_score():
    dynamic_result = {"status": "completed", "compile": {"compiles": True, "evidence_id": "E1"}, "data": {"raw_data_present": True, "evidence_id": "E2"},
                      "runs": [{"succeeded": True, "evidence_id": "E3"}, {"succeeded": False, "evidence_id": "E4"}]}
    checks = [{"matches": True, "evidence_id": "E5"}, {"matches": False, "evidence_id": "E6"}, {"matches": None}]
    summary = dynamic_execution_summary(dynamic_result, checks, [])
    assert summary["criteria"]["execution"]["fraction"] == pytest.approx((1 + 1 + 0.5) / 3, abs=1e-3)
    assert summary["criteria"]["result_reproduction"]["fraction"] == 0.5
    score = calculate_dynamic_score(summary)
    assert score.criteria_assessed == 2 and score.score == pytest.approx(4 * 2.5 / 3 + 3, abs=0.01)
    assert calculate_dynamic_score(dynamic_execution_summary({}, [], [])).score is None
    problems = [Problem(title="a", category="code_bug", severity="high", rationale="r", evidence_id="E1", quote="q"),
                Problem(title="b", category="documentation", severity="low", rationale="r", evidence_id="E1", quote="q")]
    aggregated = aggregate_problems(problems)
    assert aggregated.score == 7.5 and len(aggregated.penalties) == 2


def test_generated_scripts_are_sanitized_and_inventory_lists_column_values(tmp_path):
    assert dynamic.sanitize_script("x = None\x0cor k in y:\n    pass") == "x = None\nfor k in y:\n    pass"
    root = dynamic.extract_archive(_package(tmp_path))
    inventory = dynamic.data_inventory(root)
    assert inventory["files"][0]["column_values"] == {"problem": ["p1", "p2", "p3"], "ok": ["False", "True"]}


def test_audit_artifacts_are_not_compiled_or_run(tmp_path):
    root = dynamic.extract_archive(_package(tmp_path))
    (root / ".replicagent_checks").mkdir()
    (root / ".replicagent_checks" / "C01.py").write_text("this is not python")
    (root / "analysis" / ".replicagent_make_table.py").write_text("also not python")
    assert [f["file"] for f in dynamic.compile_check(root)["failures"]] == ["analysis/plot_broken.py"]
    assert not any(".replicagent" in str(p) for p in dynamic.find_analysis_scripts(root))


def test_package_scripts_run_as_modules_from_the_root(tmp_path, monkeypatch):
    archive = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("repo/analysis/__init__.py", "")
        zf.writestr("repo/analysis/load.py", "def rows():\n    return [1, 2]\n")
        zf.writestr("repo/analysis/plot_counts.py", "from analysis.load import rows\nimport os\nos.makedirs('out', exist_ok=True)\nopen('out/figure_counts.txt', 'w').write(str(len(rows())))\nprint('ok')\n")
        zf.writestr("repo/results.csv", "a\n1\n")
    installs = []
    monkeypatch.setattr(dynamic, "_install_missing", lambda stderr, root=None: installs.append(stderr) or None)
    root = dynamic.extract_archive(archive)
    run = dynamic.run_script(root, root / "analysis" / "plot_counts.py")
    assert run["succeeded"] and run["invocation"] == "module" and run["cwd"] == "." and run["produced_files"] == ["out/figure_counts.txt"]
    assert installs == []


def test_local_modules_are_never_pip_installed(tmp_path, monkeypatch):
    root = dynamic.extract_archive(_package(tmp_path))
    (root / "analysis" / "__init__.py").write_text("")
    calls = []
    monkeypatch.setattr(dynamic.subprocess, "run", lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})())
    assert dynamic._install_missing("ModuleNotFoundError: No module named 'analysis.endogenous'", root) is None
    assert dynamic._install_missing("ModuleNotFoundError: No module named 'plot_broken'", root) is None, "a local .py file counts as local"
    assert calls == []
    assert dynamic._install_missing("ModuleNotFoundError: No module named 'seaborn'", root) == "seaborn" and len(calls) == 1


def test_environment_failures_are_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamic, "_install_missing", lambda stderr, root=None: None)
    root = dynamic.extract_archive(_package(tmp_path))
    run = dynamic.run_script(root, root / "analysis" / "needs_module.py")
    assert not run["succeeded"] and run["environment_failure"] is True


def test_missing_input_files_are_reported_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(dynamic, "_install_missing", lambda stderr, root=None: None)
    root = dynamic.extract_archive(_package(tmp_path))
    (root / "analysis" / "plot_missing.py").write_text("import pandas as pd\npd.read_csv('data_raw/posts.csv')\n")
    run = dynamic.run_script(root, root / "analysis" / "plot_missing.py")
    assert not run["succeeded"] and run["missing_input"] == "data_raw/posts.csv" and run["environment_failure"] is False

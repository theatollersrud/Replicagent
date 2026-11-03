"""A bounded independent check of cached CSV labels. Audited code is never run."""

import csv
import io
import json
import re
import zipfile
import ast
from datetime import date, datetime
from pathlib import Path

from .schemas import ResultComparison
from .storage import EvidenceLedger


def livecodebench_selection(archive):
    """Read the authors' declared date filter through AST, without running it."""
    notebooks = [m for m in archive.infolist() if m.filename.endswith("/analysis/CreateVennDiagrams.ipynb") and m.file_size < 1_500_000]
    datasets = [m for m in archive.infolist() if m.filename.endswith("/benchmarks/livecodebench.json") and m.file_size < 5_000_000]
    if len(notebooks) != 1 or len(datasets) != 1:
        raise ValueError("The LiveCodeBench date-selection sources could not be established.")
    notebook = json.loads(archive.read(notebooks[0]))
    code = "\n".join("".join(cell.get("source", [])) for cell in notebook["cells"] if cell["cell_type"] == "code")
    selections = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "livecodebench_problem_keys_on_or_after":
            values = {item.arg: item.value.value for item in node.keywords if isinstance(item.value, ast.Constant) and type(item.value.value) is int}
            if "year" in values and "month" in values:
                selections.add((values["year"], values["month"]))
    if len(selections) != 1:
        raise ValueError("The LiveCodeBench date-selection call is ambiguous.")
    year, month = selections.pop()
    lower_bound = date(year, month, 1)
    problems = json.loads(archive.read(datasets[0]))["data"]
    selected = {key for key, info in problems.items() if info.get("contest_date") and datetime.fromisoformat(info["contest_date"].replace("Z", "+00:00")).date() >= lower_bound}
    return selected, f"The authors' notebook selects contest dates on or after {lower_bound.isoformat()}, yielding {len(selected)} problem IDs. The sources are {notebooks[0].filename} and {datasets[0].filename}."


def compare_cached_counts(audit_id: str, repository: dict, manifest) -> list[ResultComparison]:
    archive_path = repository.get("local_archive_path")
    if repository.get("status") != "inspected" or not archive_path:
        return []
    targets = {}
    for claim in manifest.claims:
        text = claim.statement.casefold()
        all_models = re.search(r"\b(?:all\s+(?:(?:ten|10)\s+)?models|ens_all)\b", text)
        if not ("ensemble" in text and any(word in text for word in ("maximum", "upper bound", "upperbound")) and all_models):
            continue
        if not re.fullmatch(r"\d+(?:\.\d+)?", claim.reported_value.strip()):
            continue
        if claim.reported_value.strip() not in claim.quote:
            continue
        for benchmark in ("humaneval", "defects4j", "livecodebench"):
            if benchmark in text.replace("-", ""):
                targets.setdefault(benchmark, []).append(claim)
    comparisons = []
    with zipfile.ZipFile(Path(archive_path)) as archive:
        for benchmark, claims in targets.items():
            suffix = f"/analysis/all_outputs_{benchmark}.csv"
            files = [m for m in archive.infolist() if m.filename.endswith(suffix) and m.file_size <= 100_000_000]
            if len(files) != 1:
                continue
            member = files[0]
            selected_problems = None
            selection_description = "The full cached benchmark file was used."
            if benchmark == "livecodebench":
                selected_problems, selection_description = livecodebench_selection(archive)
            solved = set()
            rows = 0
            with archive.open(member) as binary:
                reader = csv.DictReader(io.TextIOWrapper(binary, encoding="utf-8", newline=""))
                if not {"Problem Name", "Plausibility", "Benchmark"} <= set(reader.fieldnames or []):
                    continue
                for row in reader:
                    rows += 1
                    if rows > 200_000:
                        raise ValueError("The cached-result comparison exceeded its row budget.")
                    expected_labels = {benchmark, "lcb"} if benchmark == "livecodebench" else {benchmark}
                    if row["Benchmark"].casefold() not in expected_labels:
                        raise ValueError("The cached-result CSV contains an unexpected benchmark.")
                    if selected_problems is not None and row["Problem Name"] not in selected_problems:
                        continue
                    if row["Plausibility"].strip().casefold() in {"true", "1", "yes", "y", "plausible"}:
                        solved.add(row["Problem Name"])
            observation = json.dumps({"benchmark": benchmark, "rows": rows, "selection": selection_description, "unique_solved_problems": len(solved), "solved_problem_ids": sorted(solved)}, sort_keys=True)
            record = EvidenceLedger(audit_id).add(
                "cached_result_calculation", repository["source"], f"{repository['revision']}, {member.filename}", observation,
                "Counted unique Problem Name values with a positive Plausibility label across all models using the standard-library CSV parser. Applied the authors' documented date filter where present. Author-provided labels were not independently validated.",
            )
            comparisons.extend(ResultComparison(claim_id=claim.claim_id, benchmark=benchmark, reported_value=float(claim.reported_value), observed_value=len(solved), matches=float(claim.reported_value) == len(solved), evidence_id=record.evidence_id) for claim in claims)
    return comparisons

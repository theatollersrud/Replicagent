"""Bounded dynamic analysis. Analysis scripts are executed; experiments are never rerun.

This branch runs repository code with the project's own Python interpreter. It is intended for
packages the auditor trusts. Isolation can be added later.
"""

import ast
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from .storage import EvidenceLedger, ROOT

SCRIPT_TIMEOUT = 180
CHECK_TIMEOUT = 60
PARALLEL_RUNS = 4
MAX_SCRIPTS = 6
DATA_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".parquet", ".pkl", ".pickle", ".npy", ".npz", ".xlsx", ".h5", ".hdf5", ".feather", ".arrow"}
OUTPUT_SUFFIXES = {".pdf", ".png", ".svg", ".jpg", ".jpeg", ".csv", ".tex", ".txt", ".json", ".html"}
ANALYSIS_HINTS = ("analysis", "figure", "fig", "table", "plot", "venn", "heatmap", "compile", "results", "summar", "evaluate", "stats")
EXCLUDE_HINTS = ("inference", "train", "generate", "generation", "validation", "validate", "finetune", "fine_tune", "download", "setup", "manual")
PACKAGE_NAMES = {"sklearn": "scikit-learn", "cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "venn": "venn", "matplotlib_venn": "matplotlib-venn", "bs4": "beautifulsoup4", "skimage": "scikit-image"}


def extract_archive(archive_path: Path) -> Path:
    """Extract the audited ZIP once under inputs/work and return the package root."""
    key = hashlib.sha256(Path(archive_path).read_bytes()).hexdigest()[:16]
    destination = ROOT / "inputs" / "work" / key
    with zipfile.ZipFile(archive_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        for member in members:
            name = PurePosixPath(member.filename)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("The archive contains an unsafe file path.")
        if not destination.exists():
            destination.mkdir(parents=True)
            archive.extractall(destination)
        top = {PurePosixPath(m.filename).parts[0] for m in members}
    return destination / top.pop() if len(top) == 1 and (destination / next(iter(top))).is_dir() else destination


def _audit_artifact(path: Path) -> bool:
    """Files the audit itself writes into the package must not be inspected or run."""
    return any(part.startswith(".replicagent") for part in path.parts)


def notebook_code(text: str) -> str:
    """Join the code cells of a notebook into a script, dropping IPython magics and shell lines."""
    notebook = json.loads(text)
    lines = []
    for index, cell in enumerate(notebook.get("cells", []), 1):
        if cell.get("cell_type") != "code":
            continue
        lines.append(f"# --- cell {index} ---")
        for line in "".join(cell.get("source", [])).splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("%", "!")) or stripped.startswith("get_ipython("):
                lines.append("# skipped: " + line)
            else:
                lines.append(line)
    return "\n".join(lines) + "\n"


def compile_check(root: Path) -> dict:
    """Parse every Python file and notebook. Nothing is imported or executed."""
    failures = []
    python_files = sorted(p for p in root.rglob("*.py") if p.is_file() and not _audit_artifact(p))
    notebooks = sorted(p for p in root.rglob("*.ipynb") if p.is_file() and not _audit_artifact(p))
    imports = set()
    for path in python_files + notebooks:
        try:
            source = notebook_code(path.read_text(encoding="utf-8", errors="replace")) if path.suffix == ".ipynb" else path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except SyntaxError as exc:
            failures.append({"file": str(path.relative_to(root)), "line": exc.lineno, "message": exc.msg, "source": (exc.text or "").strip()[:160]})
            continue
        except (ValueError, KeyError) as exc:
            failures.append({"file": str(path.relative_to(root)), "line": None, "message": f"unreadable notebook: {type(exc).__name__}", "source": ""})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imports.add(node.module.split(".")[0])
    local = {p.stem for p in python_files} | {p.name for p in root.rglob("*") if p.is_dir()}
    stdlib = set(sys.stdlib_module_names)
    third_party = sorted(m for m in imports if m not in stdlib and m not in local and not m.startswith("_"))
    missing = []
    for module in third_party:
        probe = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=60)
        if probe.returncode:
            missing.append(module)
    return {
        "python_files": len(python_files), "notebooks": len(notebooks), "failures": failures,
        "compiles": not failures, "third_party_imports": third_party, "missing_modules": missing,
    }


def data_inventory(root: Path) -> dict:
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in DATA_SUFFIXES):
        entry = {"path": str(path.relative_to(root)), "bytes": path.stat().st_size}
        if path.suffix.lower() in {".csv", ".tsv"} and entry["bytes"] < 200_000_000:
            try:
                with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
                    reader = csv.reader(stream, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
                    header = next(reader, [])
                    values: dict[str, set] = {name: set() for name in header[:12]}
                    sample, rows = None, 0
                    for row in reader:
                        rows += 1
                        if sample is None:
                            sample = row
                        if rows <= 200_000:
                            for name, cell in zip(header[:12], row):
                                if name in values:
                                    values[name].add(cell[:60])
                                    if len(values[name]) > 40:
                                        del values[name]
                entry.update(columns=",".join(header)[:3000], sample_row=",".join(sample or [])[:400], rows=rows,
                             column_values={name: sorted(cells) for name, cells in values.items() if cells})
            except OSError:
                pass
        files.append(entry)
    by_suffix = {}
    for entry in files:
        suffix = PurePosixPath(entry["path"]).suffix.lower()
        by_suffix[suffix] = by_suffix.get(suffix, 0) + 1
    return {"files": files[:400], "file_count": len(files), "total_bytes": sum(f["bytes"] for f in files), "by_suffix": by_suffix, "raw_data_present": bool(files)}


def find_analysis_scripts(root: Path) -> list[Path]:
    candidates = []
    for path in sorted(list(root.rglob("*.ipynb")) + list(root.rglob("*.py"))):
        relative = str(path.relative_to(root)).casefold()
        if ".ipynb_checkpoints" in relative or _audit_artifact(path) or path.stat().st_size > 1_500_000:
            continue
        if any(hint in relative for hint in EXCLUDE_HINTS):
            continue
        if any(hint in relative for hint in ANALYSIS_HINTS):
            head = path.read_text(encoding="utf-8", errors="replace")[:20_000]
            if re.search(r"\b(?:import|from)\s+(?:torch|transformers|vllm|tensorflow|jax|accelerate|peft)\b", head):
                continue
            candidates.append(path)
    candidates.sort(key=lambda p: (0 if p.suffix == ".ipynb" else 1, len(str(p))))
    return candidates[:MAX_SCRIPTS]


SOURCE_BUDGET = 400_000
SOURCE_FILE_LIMIT = 80_000


def analysis_sources(root: Path) -> list[dict]:
    """Return the full text of analysis and metric code, notebooks reduced to their code cells, within a character budget."""
    candidates = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".py", ".ipynb", ".sh", ".r", ".jl"} and not _audit_artifact(p)):
        relative = str(path.relative_to(root)).casefold()
        if ".ipynb_checkpoints" in relative or path.stat().st_size > 5_000_000:
            continue
        rank = 0 if any(h in relative for h in ("analysis", "metric", "entropy", "score", "select", "confidence")) else 1 if any(h in relative for h in ANALYSIS_HINTS) else 2
        candidates.append((rank, len(relative), path))
    sources, used = [], 0
    for _, _, path in sorted(candidates, key=lambda item: item[:2]):
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = notebook_code(raw) if path.suffix == ".ipynb" else raw
        if path.suffix == ".ipynb" and not text.strip():
            continue
        truncated = len(text) > SOURCE_FILE_LIMIT
        text = text[:SOURCE_FILE_LIMIT]
        if used + len(text) > SOURCE_BUDGET:
            break
        used += len(text)
        sources.append({"file": str(path.relative_to(root)), "text": text, "truncated": truncated, "lines": text.count("\n") + 1})
    return sources


def _snapshot(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file() and p.suffix.lower() in OUTPUT_SUFFIXES}


def _run(command: list[str], cwd: Path, timeout: int) -> dict:
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return {"exit_code": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:], "timed_out": False, "seconds": round(time.monotonic() - started, 1)}
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": None, "stdout": (exc.stdout or b"")[-4000:].decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")[-4000:], "stderr": f"Timed out after {timeout} seconds.", "timed_out": True, "seconds": round(time.monotonic() - started, 1)}


def _install_missing(stderr: str) -> str | None:
    match = re.search(r"No module named '([A-Za-z0-9_]+)", stderr)
    if not match:
        return None
    module = match.group(1)
    package = PACKAGE_NAMES.get(module, module)
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", package], capture_output=True, text=True, timeout=240)
    return package if result.returncode == 0 else None


def run_script(root: Path, path: Path) -> dict:
    """Run one analysis notebook or script from its own directory and record what it produced."""
    if path.suffix == ".ipynb":
        script = path.with_name(f".replicagent_{path.stem}.py")
        script.write_text(notebook_code(path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    else:
        script = path
    before = _snapshot(root)
    result = _run([sys.executable, str(script)], path.parent, SCRIPT_TIMEOUT)
    installed = []
    if result["exit_code"] and "No module named" in result["stderr"]:
        package = _install_missing(result["stderr"])
        if package:
            installed.append(package)
            result = _run([sys.executable, str(script)], path.parent, SCRIPT_TIMEOUT)
    after = _snapshot(root)
    produced = sorted(p for p, stamp in after.items() if before.get(p) != stamp and not PurePosixPath(p).name.startswith(".replicagent_"))
    if path.suffix == ".ipynb":
        script.unlink(missing_ok=True)
    return {"file": str(path.relative_to(root)), "kind": "notebook" if path.suffix == ".ipynb" else "script", **result, "installed_packages": installed, "produced_files": produced[:60], "succeeded": result["exit_code"] == 0}


def sanitize_script(source: str) -> str:
    """Repair control characters that structured model output produces for `\\n` + letter sequences (`\\f` for `\\nf`)."""
    return source.replace("\x0c", "\nf").replace("\x08", "\nb").replace("\x0b", "\nv").replace("\x07", "\na").replace("\r\n", "\n")


def run_generated_check(root: Path, check: dict) -> dict:
    """Run a model-written check script; its last RESULT= line is compared with the claim."""
    checks_dir = root / ".replicagent_checks"
    checks_dir.mkdir(exist_ok=True)
    script = checks_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', check['claim_id'])}.py"
    script.write_text(sanitize_script(check["script"]), encoding="utf-8")
    result = _run([sys.executable, str(script)], root, CHECK_TIMEOUT)
    values = re.findall(r"^RESULT\s*=\s*(.+?)\s*$", result["stdout"], re.M)
    computed = values[-1] if values else None
    expected = str(check.get("expected_value", "")).strip()
    kind = check.get("kind", "claim")
    matches, cells = None, None
    if kind == "claim" and computed is not None and expected and computed.strip().upper() != "UNRESOLVED":
        matches, cells = compare_values(computed, expected)
    return {"claim_id": check["claim_id"], "kind": kind, "description": check.get("description", ""), "expected_value": expected, "computed_value": computed,
            "matches": matches, "cell_matches": cells, "script": check["script"], "script_path": str(script.relative_to(root)), **result}


def _close(left, right) -> bool:
    """Integers must match exactly; decimals may differ by 0.5 percent to absorb rounding in the paper."""
    try:
        l, r = float(left), float(right)
    except (TypeError, ValueError):
        return " ".join(str(left).split()).casefold() == " ".join(str(right).split()).casefold()
    if "." not in str(right).strip() and "." not in str(left).strip():
        return l == r
    return abs(l - r) <= max(1e-6, 0.005 * abs(r))


def compare_values(computed: str, expected: str):
    """Compare a single value, or two JSON objects cell by cell for a table sweep."""
    try:
        left, right = json.loads(computed), json.loads(expected)
    except ValueError:
        left = right = None
    if isinstance(left, dict) and isinstance(right, dict):
        cells = {key: (key in left and _close(left[key], value)) for key, value in right.items()}
        return all(cells.values()) and bool(cells), cells
    return _close(computed, expected), None


def run_dynamic_analysis(audit_id: str, repository: dict) -> dict:
    """Compile, inventory data, and run figure/table scripts concurrently. Returns a summary with evidence IDs."""
    started = time.monotonic()
    archive = repository.get("local_archive_path")
    if repository.get("status") != "inspected" or not archive:
        return {"status": "not_attempted", "reason": "No inspected package archive is available, so nothing was executed."}
    ledger = EvidenceLedger(audit_id)
    source, revision = repository["source"], repository["revision"]
    root = extract_archive(Path(archive))
    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS + 2) as pool:
        compile_future = pool.submit(compile_check, root)
        data_future = pool.submit(data_inventory, root)
        scripts = find_analysis_scripts(root)
        run_futures = [pool.submit(run_script, root, path) for path in scripts]
        compiled, data = compile_future.result(), data_future.result()
        runs = [future.result() for future in run_futures]
    sources = analysis_sources(root)
    for item in sources:
        record = ledger.add("analysis_source", source, f"{revision}, {item['file']}, lines 1-{item['lines']}", item["text"], "Read the complete source text from the extracted package; notebooks reduced to their code cells." + (" The file was truncated to the per-file limit." if item["truncated"] else ""))
        item["evidence_id"] = record.evidence_id
    compile_record = ledger.add("dynamic_compile", source, revision, json.dumps(compiled, ensure_ascii=False, sort_keys=True), "Parsed every Python file and notebook with the audit interpreter's AST and probed third-party imports. Nothing was executed.")
    data_record = ledger.add("data_inventory", source, revision, json.dumps({k: v for k, v in data.items() if k != "files"} | {"files": data["files"][:120]}, ensure_ascii=False, sort_keys=True), "Listed data files by extension and counted CSV rows with a streaming reader.")
    for run in runs:
        record = ledger.add("script_execution", source, f"{revision}, {run['file']}", json.dumps(run, ensure_ascii=False, sort_keys=True), f"Executed the file with {sys.executable} from its own directory under a {SCRIPT_TIMEOUT} second timeout and listed files it created or modified.")
        run["evidence_id"] = record.evidence_id
    return {
        "status": "completed", "package_root": str(root), "seconds": round(time.monotonic() - started, 1),
        "compile": {**compiled, "evidence_id": compile_record.evidence_id},
        "data": {**{k: v for k, v in data.items() if k != "files"}, "files": data["files"][:120], "evidence_id": data_record.evidence_id},
        "scripts_found": [str(p.relative_to(root)) for p in scripts], "runs": runs, "analysis_sources": sources,
        "limitations": [
            "Repository code was executed with the audit's own Python interpreter without isolation. This is intended for trusted packages.",
            f"At most {MAX_SCRIPTS} analysis notebooks or scripts were run, each under a {SCRIPT_TIMEOUT} second timeout. Inference, training, and generation scripts were not run.",
            "A produced file shows that a script ran. Whether its content matches the paper requires the comparison recorded in the report.",
        ],
    }


def clear_checks(root: Path):
    checks_dir = root / ".replicagent_checks"
    for stale in checks_dir.glob("*.py") if checks_dir.exists() else []:
        stale.unlink()


def run_generated_checks(audit_id: str, repository: dict, dynamic: dict, checks: list[dict], round_number: int = 1) -> list[dict]:
    """Run a batch of model-written checks in parallel and record each as evidence."""
    if dynamic.get("status") != "completed" or not checks:
        return []
    root = Path(dynamic["package_root"])
    ledger = EvidenceLedger(audit_id)
    if round_number == 1:
        clear_checks(root)
    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        results = list(pool.map(lambda check: run_generated_check(root, {**check, "claim_id": f"{check['claim_id']}_r{round_number}" if round_number > 1 else check["claim_id"]}), checks))
    for check, result in zip(checks, results):
        result["claim_id"] = check["claim_id"]
        result["round"] = round_number
        observation = json.dumps({k: v for k, v in result.items() if k != "script"} | {"script": result["script"][:6000]}, ensure_ascii=False, sort_keys=True)
        record = ledger.add("generated_check", repository["source"], f"{repository['revision']}, {result['script_path']}", observation, f"Executed a model-written check script under a {CHECK_TIMEOUT} second timeout and read its final RESULT line (round {round_number}).")
        result["evidence_id"] = record.evidence_id
    return results

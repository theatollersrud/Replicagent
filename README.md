# Replicagent

Replicagent is a local Google Agent Development Kit (ADK) demo that reads a research paper, discovers a replication package, inspects its source files, and saves an evidence-grounded static reproducibility report.

## Start the demo

Open your Ubuntu WSL terminal and run these commands from this project folder.

```bash
cd /mnt/c/Users/prebe/Documents/chatgpt-projects/hackathon
adk web --port 8000
```

Open http://localhost:8000 and select **replicagent** in the application selector. Send this prompt.

```text
Audit https://arxiv.org/abs/2510.21513v2
```

On this branch the workflow has nine visible stages and normally makes four Gemini calls. The first five are the static audit from `main`.

1. `IngestPaper` reads the paper and records its source evidence. For arXiv papers it downloads the LaTeX source first and falls back to PDF text extraction only when arXiv offers no source.
2. `UnderstandPaper` extracts empirical claims using Gemini and a typed output schema.
3. `InspectArtifacts` discovers the replication package and inspects source files without executing them.
4. `ReviewEvidence` interprets the evidence and proposes reproduction steps using Gemini.
5. `DynamicAnalysis` collects the compile check, data inventory, analysis script runs, and reference lookups. These start in the background as soon as static inspection finishes, so they overlap with the `ReviewEvidence` model call.
6. `GenerateChecks` asks Gemini to write small pandas scripts that recompute verified claims from the stored data.
7. `RunChecks` runs those scripts in parallel and compares each `RESULT=` line with the paper's value.
8. `AssessCorrectness` asks Gemini whether the code implements what the paper says, lists problems with severity, and judges whether cited sentences match the reference titles.
9. `VerifyAndScore` checks source quotes, independently counts supported cached results, calculates the static, dynamic, and aggregated scores, and saves the final report.

Pass `--static-only` to the terminal runner to stop after the static audit.

The supplied paper links to an anonymized Figshare package. The discovery fallback searches Zenodo by title and requires metadata containing the exact arXiv identifier. It can find the later public package at https://zenodo.org/records/19564925. The report records that this package may differ from the paper's original package.

Artifact discovery also recognizes Zenodo DOI links and follows metadata redirects after checking each destination. GitHub revisions are retrieved through Git, which avoids the unauthenticated GitHub API quota. If inspection is blocked, the interface displays the specific reason.

To select that package explicitly, send this prompt.

```text
Audit https://arxiv.org/abs/2510.21513v2 using https://zenodo.org/records/19564925
```

The first run downloads the paper and replication package. Later runs reuse those downloads. Each run performs fresh model analysis and saves a new audit. Cached downloads do not imply cached model responses.

## Read the result

The final report appears in the chat. Each audit also creates the following files.

```text
audits/<audit_id>/
  status.json
  paper.json
  manifest.json
  repository.json
  evidence.jsonl
  rejected.json
  report.json
  report.md
```

The evidence ledger is append-only. It records source locations, observations, hashes, methods, and timestamps. Reports cite evidence IDs and short quotes. The evidence check verifies that the quotes occur in the stored source observations. An agent's interpretation of a quote still requires review.

The static and dynamic assessments each have their own 0–10 score and weights in `configs/scoring.json`. The static score covers code, stored artifacts, documentation, configuration, and environment indicators. The dynamic score covers execution and reproduction of reported results. Each assessment reports its own coverage and unresolved points. The report has no combined score. A missed static indicator means that the indicator was not detected in the inspected package. The rubric is a coarse demonstration policy that needs expert calibration.

On `main`, dynamic execution is unattempted and the dynamic score is displayed as **Not assessed**, with JSON value `null`. On the `dynamic-analysis` branch, the report adds sections for the dynamic analysis, generated checks, references, correctness problems, citation support, and the aggregated score. This preserves the distinction between an unattempted test and a demonstrated failure. Mathematical correctness and reference verification are also untested.

For the supplied replication package, the report can independently count the union of solved problems across all models from the authors' cached CSV labels. It can compare those counts with extracted theoretical-maximum claims. It reads the LiveCodeBench date filter from the authors' analysis notebook through Python's source parser and applies that filter to the benchmark metadata. It does not execute the notebook. These checks test consistency with stored labels and do not independently establish that the generated programs pass their tests.

## Dynamic analysis on this branch

The dynamic stage never reruns experiments. It extracts the package under `inputs/work/`, parses every Python file and notebook, lists the data files with CSV headers and row counts, and runs at most six figure or table notebooks or scripts. Notebooks are converted to scripts with IPython magics removed and run from their own directory with the Matplotlib Agg backend under a 180 second timeout. Files that scripts create or modify are listed as produced outputs. Scripts that import PyTorch, Transformers, or similar stacks, and files whose names suggest inference, training, or generation, are skipped. A missing module triggers one `pip install` into the audit environment and one retry.

Repository code runs with the audit's own interpreter and no isolation. This branch is meant for papers and packages that the auditor trusts. Isolation can be added later.

Reference checking parses `\bibitem` entries from the inlined `.bbl`. DOIs are resolved on Crossref, arXiv identifiers (including `10.48550/arXiv.` DOIs) on the arXiv API, and remaining entries are searched on Crossref by title. Lookups run three at a time with a 60 second budget and are never retried. The first failure, including a `429 Too Many Requests` response, stops the remaining lookups, and the report states that reference checking is not fully completed. A lookup miss is reported as a problem, not as proof of a fabricated reference.

The correctness review is a single Gemini call over the paper source, the code excerpts, the dynamic observations, the generated check results, and the reference results. Every problem must quote an evidence record, and quotes are checked mechanically. Parse failures, failed script runs, unresolved references, and mismatched generated checks are added as deterministic problems before the model's interpretations. The aggregated score starts at 10 and subtracts 3 per critical, 2 per high, 1 per medium, and 0.5 per low problem. It is a provisional triage number.

The dynamic score keeps its two criteria. Execution averages whether the package parses, whether stored data exist, and the fraction of analysis scripts that ran. Result reproduction is the fraction of cached-label comparisons and generated checks that matched the paper.

## LaTeX sources are preferred

For an arXiv URL, the demo downloads the e-print at `https://arxiv.org/e-print/<id>` before the PDF. The LaTeX source keeps every table row intact, whereas PDF text extraction can garble table markers and superscripts. The e-print is read as a gzip or tar archive, the file containing `\documentclass` is selected, and `\input` and `\include` references to files in the archive are inlined so that one document holds every table. The flattened source is saved under `inputs/` and recorded in the evidence ledger with the method `LaTeX source read`. When arXiv has no LaTeX source, the audit records that in `paper.json` under `ingestion_notes` and uses the PDF.

The claim extractor is told when it reads LaTeX. It is informed that cells are separated by `&`, that `\textbf{122}` is 122, that `\hphantom{1}89` is 89, and that `$^{(-6.35)}$` is a superscript annotation. The evidence check compares LaTeX quotes on their visible content, so a quote written as `64^{(-6.35)}` verifies against a source line containing `64$^{(-6.35)}$`. Numbers must still appear in the same order; the check only ignores `$`, braces, and formatting commands. This tolerance applies to LaTeX evidence only. PDF evidence is still matched on its exact extracted text.

## Use another paper

The web prompt accepts an arXiv URL (LaTeX source preferred, PDF fallback), a public PDF URL, and an optional public GitHub or Zenodo repository URL. PDF attachments are supported when ADK provides their bytes as an inline PDF attachment. A local `.tex` file can be submitted through the terminal runner or API after copying it into the project folder. The demo reads the single LaTeX source file without compiling it or resolving includes.

```bash
python3.12 -m scripts.run_audit https://arxiv.org/abs/2510.21513v2
python3.12 -m scripts.run_audit inputs/paper.tex --repository https://github.com/OWNER/REPOSITORY
```

The demo reads at most 80 PDF pages and supplies at most 180,000 paper-text characters to the model. It inspects public GitHub source archives at a recorded commit, or public Zenodo ZIP files at a recorded version and archive hash. It downloads at most 320 MiB per package and reads at most 20 MB of small source files for static inspection. Supported cached-result checks separately read up to three CSV files of at most 100 MB each and 200,000 rows each. Other large data files are indexed and remain unverified. Private repositories and unsupported artifact hosts are recorded as blockers.

## Installation and model access

The prepared WSL environment already has Python 3.12, ADK 2.13.0, and the required Google Cloud login. The PDF reader was installed for this demo. The configured project is `agentic-oslo-team-12-9afd`, the location is `global`, and the default model is `gemini-3.5-flash`.

For a new environment, create a virtual environment and install the project.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,dynamic]'
cp .env.example .env
gcloud auth application-default login
adk web --port 8000
```

The model client uses application default credentials when configured. In the prepared local WSL environment, it otherwise obtains short-lived tokens from the existing `gcloud` login. It keeps those tokens in memory. It does not write or print tokens. Model calls use the active Google Cloud project's billing.

The workflow leaves model reasoning and output-token settings at the provider defaults. It imposes no reasoning budget or output-token cap. The local `gcloud` credential adapter asks the CLI for a token for each model request, so it does not assume an additional token lifetime. If a model returns invalid structured output or an authentication error, the affected stage retries once. A persistent failure records the specific error and marks the audit as blocked, while preserving the paper and existing evidence for diagnosis.

You can change the model with `AUDITOR_MODEL` and the project with `GOOGLE_CLOUD_PROJECT`. The `.env.example` file documents the available settings.

## Run the tests

```bash
python3.12 -m pytest -q
```

The tests use synthetic fixtures and make no paid model calls. They cover unresolved scores, missing-code caps, invalid evidence rejection, archive traversal checks, static syntax inspection without execution, cached-result counting, arXiv LaTeX source selection with PDF fallback, LaTeX-aware quote matching, notebook conversion and execution, generated check comparison, bibliography parsing, reference lookups that stop at the first failure, and the local API lifecycle.

## Use the optional API

Start the separate API on another port.

```bash
python3.12 -m uvicorn replicagent.app:app --host 127.0.0.1 --port 8001
```

The API exposes these routes.

```text
POST /audits
GET  /audits/{audit_id}
GET  /audits/{audit_id}/claims
GET  /audits/{audit_id}/findings
GET  /audits/{audit_id}/executions
GET  /audits/{audit_id}/score
GET  /audits/{audit_id}/report
GET  /audits/{audit_id}/report?format=markdown
```

The `/score` endpoint returns `static_score` and `dynamic_score`, each with its own breakdown. New JSON reports use schema version `2.0`.

Submit `{"paper_source":"https://arxiv.org/abs/2510.21513v2"}` for a live audit. Submit `{"mock":true}` for the explicitly labelled synthetic offline workflow. `POST /audits` returns an audit ID immediately. Progress and partial results become available as stages finish. The API uses an in-process background task and does not resume interrupted jobs.

## Scope and deviations from the full specification

This demo combines a small foundation with real PDF ingestion, limited artifact discovery, static inspection, and two functioning Gemini agents. It uses current ADK `Workflow` graph edges instead of the older sequential-workflow abstraction. It uses local files instead of PostgreSQL and cloud object storage, so it requires no infrastructure deployment.

The demo does not implement isolated code execution, execution-based result reproduction, full training runs, repository repair, source-archive paper ingestion, LaTeX include resolution, mathematical verification, or comprehensive reference verification. The execution route reports `not_attempted`. Docker is unavailable in the prepared WSL environment. Source code from audited packages is never imported or executed by the workflow.

The implementation concentrates on a working demonstration within the requested time limit. It goes beyond the specification's mock-only first milestone to support the supplied paper, while leaving the larger execution and deployment system for later work.

## Project files

```text
replicagent/
  agent.py       # The ADK workflow and model instructions live here.
  auth.py        # The Google Cloud model client is configured here.
  runtime.py     # Model retries and persistent failure reporting live here.
  schemas.py     # Typed paper, finding, evidence, and report models live here.
  services.py    # Paper ingestion, discovery, and static inspection live here.
  storage.py     # Local audit storage and the evidence ledger live here.
  scoring.py     # The deterministic scoring engine lives here.
  comparison.py  # Independent checks of supported cached results live here.
  reporting.py   # Evidence checks and report generation live here.
  mock.py        # The explicitly labelled offline fixture workflow lives here.
  app.py         # The optional local API lives here.
configs/scoring.json
scripts/run_audit.py
tests/test_auditor.py
pyproject.toml
.env.example
```

The ADK APIs were checked against the installed package and the official documentation at https://adk.dev/agents/llm-agents/ and https://adk.dev/agents/workflow-agents/sequential-agents/.

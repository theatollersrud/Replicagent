# Replicagent

This feature branch adds an agent that tries to run code from the repository attached to the current paper, with a five-minute execution budget in Google Cloud. Open [Replicagent Dynamic (preview)](https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/europe-west4/agent-engines/8845588637643964416/playground?project=agentic-oslo-team-12-9afd) and submit a paper URL, optionally with its repository URL. The original static demo remains available through the main-branch link below.

The execution agent receives the current paper's claims, the inspected repository files and source excerpts, and the static review. It chooses commands for that repository, observes their output, and chooses the next command while time remains. It can inspect files, install dependencies, run supplied tests, or attempt a small example or experiment. It can respond to a missing dependency or a failed command by preparing the environment or choosing another relevant command. The selection contains no prescribed paper, notebook, script, or dataset.

Large repositories use a selected file inventory and source excerpts for each planning step. The full audit context and captured command observations remain available inside the execution environment for targeted reads. Older output is summarized in later requests, with recent failure details retained. The final serialized input is checked before it is sent, and a provider input-size rejection triggers one smaller-context retry. These changes do not set output-token or reasoning limits. The previously failing 6,480-file repository was replayed successfully through execution: its full test-suite attempt reported 629 passed, 1 failed, and 2 skipped. The cloud worker completed in 260.39 seconds. This is test execution evidence, not verification of the paper's scientific results.

The workflow uploads the inspected archive to a private Cloud Storage bucket and starts the Cloud Run job named `replicagent-dynamic`. The job checks the archive hash, rejects unsafe archive paths, and runs repository commands inside an isolated Cloud Run sandbox. Public outbound network access allows dependency installation and public data downloads. Repository commands receive no inherited host credentials or credential mount. The trusted controller keeps its Google Cloud credentials outside the execution sandbox.

Cloud Run enforces a hard 300-second task timeout and zero retries. This budget includes preparation, execution-agent decisions, and repository commands. The worker reserves time to save its results before the deadline. Package upload, cloud startup, and result retrieval can add time outside that budget. The agent attempts work that fits the available time and records remaining prerequisites and interrupted commands.

The report keeps the static and dynamic scores separate. Execution can contribute up to 4/10 to the provisional dynamic score. The six points for reproducing reported results remain unassessed unless a separate result comparison supports them. Dependency installation or environment inspection alone earns no execution points. The report records each command, its purpose and working directory, its exit status, elapsed time, timeout, and captured output. Program output is labelled as untrusted, and a successful command alone does not establish that a paper's result was reproduced.

The cloud worker is built with `Dockerfile.dynamic-worker` and `cloudbuild.dynamic.yaml`. The managed preview agent is deployed with `scripts/deploy_runtime.py` using `--display-name` and `--dynamic-job-name`. Execution requests and results are stored under `dynamic/<request_id>/` in the private project bucket. The original main deployment remains unchanged.

Cloud validation on 2026-11-03 passed 91 automated checks. Live cloud checks exercised an unrelated shell package, installed a missing dependency and reran unit tests, retained a genuine runtime error, and identified an interrupted command as a timeout. The original-paper integration run also selected and executed the pairwise-comparison notebook after resolving its missing kernel. A later analysis attempt lacked an input file, and a subsequent full-archive extraction stopped the execution environment. That run is partial execution evidence, not confirmation of the paper's results. The worker now stops when its execution environment is lost and directs the agent toward smaller available inputs.

The cloud worker currently provides two CPU cores and 4 GiB of memory. GPU runs, large datasets, and long training jobs can remain blocked within the five-minute budget. Run `python3.12 scripts/check_dynamic_cloud.py` through the authenticated WSL terminal to repeat the synthetic checks. This script prepares source archives and submits jobs. The test code executes only in Google Cloud.

Replicagent is a Google Agent Development Kit (ADK) demo that reads a research paper, discovers a replication package, inspects its source files, and saves an evidence-grounded static reproducibility report.

## Use the shared Google Cloud agent

Open [Replicagent's Playground](https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/europe-west4/agent-engines/649037315829661696/playground?project=agentic-oslo-team-12-9afd) while signed into your project account. You can also find **Replicagent** under **Agent Platform → Deployments** in project `agentic-oslo-team-12-9afd`. Select the `europe-west4` region if the console asks for a region.

Send `Audit https://arxiv.org/abs/2510.21513v2` in the Playground. The agent and Gemini calls run in Google Cloud. Project users need permission to view and query Agent Runtime. Project Editor and Agent Platform Administrator access cover the current demo account. Access for a different account depends on that account's assigned permissions.

Agent Studio's designer list contains agents created through the designer. This code-defined agent is accessed through Deployments and its Playground.

The managed deployment uses Google Cloud session storage. Final chat reports remain in their sessions. Intermediate audit files and downloaded packages use the container filesystem and can disappear when the container is replaced. The separate Cloud Run deployment mounts a Cloud Storage bucket for its audit files.

## Deploy an updated cloud version

The deployment metadata is recorded in `cloud-deployment.json`. The deployment helper uploads an explicit list of source files. Agent Runtime builds the Dockerfile and runs the unit tests inside Google Cloud. This command updates the managed agent. It requires a Google Cloud login and the deployment dependencies, available through `pip install '.[cloud]'` in a virtual environment.

```bash
python scripts/deploy_runtime.py \
  --project=agentic-oslo-team-12-9afd --region=europe-west4 \
  --runtime-name=projects/674330246692/locations/europe-west4/reasoningEngines/649037315829661696 \
  --service-account=674330246692-compute@developer.gserviceaccount.com \
  --artifact-bucket=agentic-oslo-team-12-9afd-replicagent
```

The deployment helper uses application default credentials or the deploying user's existing `gcloud` login. It keeps access tokens in memory. The deployed agent uses its Google Cloud service account.

## Run a local development copy

Open your Ubuntu WSL terminal and run these commands from this project folder.

```bash
cd /mnt/c/Users/prebe/Documents/chatgpt-projects/hackathon
adk web --port 8000
```

Open http://localhost:8000 and select **replicagent** in the application selector. Send this prompt.

```text
Audit https://arxiv.org/abs/2510.21513v2
```

The static workflow has five visible stages. It normally makes two Gemini calls. The cloud dynamic preview adds an execution stage between `ReviewEvidence` and `VerifyAndScore`, with further Gemini calls to choose commands based on their observed output.

1. `IngestPaper` reads the paper and records its source evidence. For arXiv papers it downloads the LaTeX source first and falls back to PDF text extraction only when arXiv offers no source.
2. `UnderstandPaper` extracts empirical claims using Gemini and a typed output schema.
3. `InspectArtifacts` discovers the replication package and inspects source files without executing them.
4. `ReviewEvidence` interprets the evidence and proposes reproduction steps using Gemini.
5. `VerifyAndScore` checks source quotes, independently counts supported cached results, calculates separate static and dynamic scores, and saves the final report.

The supplied paper links to an anonymized Figshare package. The discovery fallback searches Zenodo by title and requires metadata containing the exact arXiv identifier. It can find the later public package at https://zenodo.org/records/19564925. The report records that this package may differ from the paper's original package.

Artifact discovery also recognizes Zenodo DOI links and follows metadata redirects after checking each destination. GitHub revisions are retrieved through Git, which avoids the unauthenticated GitHub API quota. If inspection is blocked, the interface displays the specific reason.

To select that package explicitly, send this prompt.

```text
Audit https://arxiv.org/abs/2510.21513v2 using https://zenodo.org/records/19564925
```

The first run downloads the paper and replication package. Later runs reuse those downloads. Each run performs fresh model analysis and saves a new audit. Cached downloads do not imply cached model responses.

Every audit requires a paper supplied in the current request. The agent accepts arXiv identifiers, paper URLs (including PDF download links with query parameters), and PDF attachments. It announces the selected source before reading it. If the input is missing, ambiguous, or unreadable, the workflow stops and explains what is needed. It never substitutes the example paper or reuses a previous request's paper automatically.

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

On `main`, dynamic execution is unattempted and the dynamic score is displayed as **Not assessed**, with JSON value `null`. This preserves the distinction between an unattempted test and a demonstrated failure. Mathematical correctness and reference verification are also untested.

For the supplied replication package, the report can independently count the union of solved problems across all models from the authors' cached CSV labels. It can compare those counts with extracted theoretical-maximum claims. It reads the LiveCodeBench date filter from the authors' analysis notebook through Python's source parser and applies that filter to the benchmark metadata. It does not execute the notebook. These checks test consistency with stored labels and do not independently establish that the generated programs pass their tests.

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
python -m pip install -e '.[test]'
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

The tests use synthetic fixtures and make no paid model calls. They cover unresolved scores, missing-code caps, invalid evidence rejection, archive traversal checks, static syntax inspection without execution, cached-result counting, arXiv LaTeX source selection with PDF fallback, LaTeX-aware quote matching, and the local API lifecycle.

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

This demo combines real PDF ingestion, limited artifact discovery, static inspection, and two functioning Gemini agents. It uses current ADK `Workflow` graph edges. It can run locally, in Cloud Run, or in Agent Runtime with the shared console Playground. Audit files use the filesystem. Managed Agent Runtime sessions use Google Cloud session storage, and the Cloud Run deployment stores audit files on a mounted Cloud Storage bucket.

This feature branch attempts agent-selected repository commands inside an isolated cloud job. The five-minute budget limits how much dependency installation, testing, inference, or training can finish. Missing data, unavailable hardware, unsupported environments, and long-running experiments can leave execution incomplete. The report records what was attempted and observed. Comprehensive experimental reproduction, mathematical verification, and reference verification remain outside the prototype. The main demo remains available separately.

The dynamic implementation selects commands from each supplied package. Its execution results require review, and successful execution alone does not establish scientific correctness. Dynamic execution remains on this feature branch so that the static main demo can be used separately.

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
  dynamic.py     # The current package is submitted to the cloud worker here.
  execution_planner.py # The execution agent chooses its next command here.
  dynamic_worker.py # Repository commands run in an isolated cloud sandbox here.
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

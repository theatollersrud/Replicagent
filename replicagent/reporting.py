import json

from pathlib import Path, PurePath

from .schemas import AuditReport, AuditFinding, CorrectnessReview, PaperManifest, Problem, Review
from .comparison import compare_cached_counts
from .scoring import aggregate_problems, calculate_static_score, calculate_dynamic_score, dynamic_execution_summary
from .storage import EvidenceLedger, audit_dir, read, save, verify_quote


def validate_manifest(audit_id: str, value) -> tuple[PaperManifest, list[str]]:
    if isinstance(value, str):
        value = json.loads(value)
    manifest = PaperManifest.model_validate(value)
    evidence = {e.evidence_id: e for e in EvidenceLedger(audit_id).records()}
    rejected = []
    accepted = []
    for claim in manifest.claims:
        if verify_quote(evidence, claim.evidence_id, claim.quote):
            accepted.append(claim)
        else:
            rejected.append(f"Claim {claim.claim_id} had an unverifiable source quote.")
    manifest.claims = accepted
    return manifest, rejected


def _optional(audit_id: str, name: str, default):
    path = audit_dir(audit_id) / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def deterministic_problems(dynamic: dict, references: dict, checks: list[dict], comparisons: list = ()) -> list[Problem]:
    """Problems that follow directly from recorded observations, without model interpretation."""
    problems = []
    if dynamic.get("status") == "completed":
        for failure in dynamic["compile"]["failures"]:
            problems.append(Problem(title=f"{failure['file']} does not parse", category="code_bug", severity="medium", basis="observation",
                                    rationale=f"The interpreter reported '{failure['message']}' at line {failure['line']}. The file cannot run until it is fixed.",
                                    evidence_id=dynamic["compile"]["evidence_id"], quote=failure["file"], location=f"{failure['file']}:{failure['line']}"))
        for run in dynamic.get("runs", []):
            if not run["succeeded"]:
                tail = run["stderr"].strip().splitlines()[-1][:200] if run["stderr"].strip() else "no error output"
                problems.append(Problem(title=f"{run['file']} did not run to completion", category="execution_failure", severity="medium", basis="observation",
                                        rationale=f"The {run['kind']} exited with {'a timeout' if run['timed_out'] else 'code ' + str(run['exit_code'])}: {tail}",
                                        evidence_id=run["evidence_id"], quote=run["file"], location=run["file"]))
        if not dynamic["data"]["raw_data_present"]:
            problems.append(Problem(title="No stored data files were found in the package", category="missing_data", severity="high", basis="observation",
                                    rationale="Tables and figures cannot be regenerated without the stored results.", evidence_id=dynamic["data"]["evidence_id"], quote='"raw_data_present": false'))
    confirmed = {c.claim_id for c in comparisons if c.matches}
    for check in checks:
        if check.get("matches") is False and check["claim_id"] not in confirmed:
            problems.append(Problem(title=f"Claim {check['claim_id']} recomputed from stored data differs from the paper", category="result_mismatch", severity="medium", basis="observation",
                                    rationale=f"The check computed {check['computed_value']} but the paper reports {check['expected_value']}. The check script was model-written, so the filter it applied needs review before this counts against the paper.",
                                    evidence_id=check["evidence_id"], quote=f'"computed_value": "{check["computed_value"]}"', claim_ids=[check["claim_id"]], location=check["script_path"]))
    counts = references.get("counts", {})
    if counts.get("not_found"):
        missing = [r["key"] for r in references["references"] if r["status"] == "not_found"]
        problems.append(Problem(title=f"{counts['not_found']} reference(s) could not be resolved", category="reference_missing", severity="medium", basis="observation",
                                rationale="The DOI, arXiv identifier, or title lookup returned no matching record: " + ", ".join(missing[:10]) + ". A lookup miss does not prove the reference is fabricated.",
                                evidence_id=references["evidence_id"], quote='"not_found"'))
    if counts.get("title_mismatch"):
        problems.append(Problem(title=f"{counts['title_mismatch']} reference(s) resolve to a different title", category="reference_missing", severity="low", basis="observation",
                                rationale="The identifier resolves, but the registered title differs from the cited title. Check the identifier.", evidence_id=references["evidence_id"], quote='"title_mismatch"'))
    return problems


def finalize(audit_id: str, review_value, correctness_value=None) -> tuple[AuditReport, str]:
    if isinstance(review_value, str):
        review_value = json.loads(review_value)
    review = Review.model_validate(review_value)
    save(audit_id, "review.json", review.model_dump())
    if isinstance(correctness_value, str):
        correctness_value = json.loads(correctness_value)
    correctness = CorrectnessReview.model_validate(correctness_value) if correctness_value else None
    if correctness:
        save(audit_id, "correctness.json", correctness.model_dump())
    dynamic = _optional(audit_id, "dynamic.json", {})
    references = _optional(audit_id, "references.json", {})
    checks = _optional(audit_id, "checks.json", [])
    manifest = PaperManifest.model_validate(read(audit_id, "manifest.json"))
    repository = read(audit_id, "repository.json")
    evidence = {e.evidence_id: e for e in EvidenceLedger(audit_id).records()}
    rejected = read(audit_id, "rejected.json")
    findings = []
    claim_ids = {claim.claim_id for claim in manifest.claims}
    for finding in review.findings:
        if verify_quote(evidence, finding.evidence_id, finding.quote) and set(finding.claim_ids) <= claim_ids:
            finding.basis = "interpretation"
            finding.verification_status = "source_quote_verified_interpretation_unverified"
            findings.append(finding)
        else:
            rejected.append(f"Finding '{finding.title}' had an invalid quote, evidence ID, or claim ID.")
    for error in repository.get("syntax_errors", []):
        if any(f.evidence_id == error["evidence_id"] for f in findings):
            continue
        findings.append(AuditFinding(
            title=f"The source parser reports a syntax error in {error['file'].split('/')[-1]}",
            severity="medium", explanation=f"The local Python source parser reported {error['message']} at line {error['line']}. This observation concerns source parsing under the audit's Python version.",
            evidence_id=error["evidence_id"], quote=error["quote"], claim_ids=[],
            reproducibility_impact="The affected script requires investigation before inference can be rerun under this Python version. The cached analysis route may still be usable.",
            remediation="Inspect the cited source line and confirm the intended Python version before attempting inference.",
            basis="observation", verification_status="verified_static_parser_observation",
        ))
    plan = []
    for step in review.reproduction_plan:
        if step.evidence_ids and all(eid in evidence for eid in step.evidence_ids) and all(path in repository.get("file_paths", []) for path in step.relevant_files):
            plan.append(step)
        else:
            rejected.append(f"Plan for '{step.target}' had missing or invalid evidence IDs or file paths.")
    comparison_limitations = []
    try:
        comparisons = compare_cached_counts(audit_id, repository, manifest)
    except Exception as exc:
        comparisons = []
        comparison_limitations.append(f"The cached-result calculation was blocked: {type(exc).__name__}: {str(exc)[:200]}")
    evidence = {e.evidence_id: e for e in EvidenceLedger(audit_id).records()}
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "informational": 4}
    findings.sort(key=lambda finding: rank[finding.severity])
    problems = deterministic_problems(dynamic, references, checks, comparisons)
    citation_checks = []
    recorded_files = {Path(p.location.split(":")[0]).name.casefold() for p in problems if p.location}
    if correctness:
        for problem in correctness.problems:
            haystack = f"{problem.title} {problem.location} {problem.quote}".casefold()
            if problem.category in {"code_bug", "execution_failure"} and any(name and name in haystack for name in recorded_files):
                continue  # already recorded as a deterministic observation
            if verify_quote(evidence, problem.evidence_id, problem.quote) and set(problem.claim_ids) <= claim_ids:
                problem.basis = "interpretation"
                problems.append(problem)
            else:
                rejected.append(f"Problem '{problem.title}' had an invalid quote, evidence ID, or claim ID.")
        citation_checks = correctness.citation_checks
    problems.sort(key=lambda problem: rank[problem.severity])
    dynamic_ran = dynamic.get("status") == "completed"
    reference_status = references.get("status", "not_attempted")
    # Keep the large index and excerpts in separate stored files.
    repository_summary = {k: v for k, v in repository.items() if k not in {"source_excerpts", "file_paths"}}
    report = AuditReport(
        audit_id=audit_id, state="completed_dynamic_audit" if dynamic_ran else "completed_static_audit", paper=manifest,
        repository=repository_summary, findings=findings, reproduction_plan=plan,
        static_score=calculate_static_score(repository), dynamic_score=calculate_dynamic_score(dynamic_execution_summary(dynamic, checks, comparisons)),
        dynamic_status="partially_verified" if dynamic_ran else "not_attempted",
        scientific_correctness_status="partially_verified" if correctness else "not_attempted",
        reference_verification_status=reference_status if reference_status in {"not_attempted", "blocked", "failed", "partially_verified", "verified"} else "not_attempted",
        dynamic_analysis={k: v for k, v in dynamic.items() if k not in {"data"}} | ({"data": {k: v for k, v in dynamic["data"].items() if k != "files"}} if dynamic_ran else {}),
        references={k: v for k, v in references.items() if k != "references"} | ({"unresolved": [r for r in references.get("references", []) if r["status"] in {"not_found", "title_mismatch"}]} if references else {}),
        generated_checks=[{k: v for k, v in check.items() if k not in {"stdout", "stderr"}} for check in checks],
        problems=problems, citation_checks=citation_checks,
        correctness_summary=correctness.summary if correctness else "", implementation_matches_paper=correctness.implementation_matches_paper if correctness else "unknown",
        aggregated_score=aggregate_problems(problems) if (dynamic_ran or correctness) else None,
        summary=(f"The audit inspected {repository.get('file_count', 0)} artifact files and retained {len(findings)} findings with checked source references. "
                 f"It completed independent comparisons for {len(comparisons)} extracted claims using author-provided cached labels. "
                 "The findings below separate deterministic observations from agent interpretations. Full experimental reproduction remains untested."),
        claim_coverage={"claims_extracted": len(manifest.claims), "claims_numerically_verified": sum(c.matches for c in comparisons), "claims_compared_against_cached_labels": len(comparisons), "fraction_numerically_verified": sum(c.matches for c in comparisons) / len(manifest.claims) if manifest.claims else None, "scope": "Independent checks of stored labels. Original code generation and validation remain untested."},
        cached_result_comparisons=comparisons,
        limitations=[
            ("Analysis scripts were executed, but experiments were not rerun. Regenerated figures and recomputed values test consistency with stored data only." if dynamic_ran
             else "Repository code was not executed. Independent checks of cached labels, when available, do not establish full experimental reproduction."),
            *([f"Reference checking is not fully completed: {references['reason']}"] if references and not references.get("complete") and references.get("reason") else []),
            *(["Reference checking was not attempted."] if not references else []),
            *(correctness.limitations if correctness else []),
            *dynamic.get("limitations", []),
            "Source quotes and evidence IDs were checked mechanically. The reasoning connecting a quote to an interpretation still requires review.",
            "The scoring rubric checks coarse artifact and documentation indicators. It has not been calibrated against expert assessments.",
            "Mathematical and citation-support judgements come from a model reading the paper and code. They are interpretations, not proofs.",
            "Discovered artifact versions may postdate the audited paper. Their correspondence to the exact paper version requires confirmation.",
            *repository.get("limitations", []), *comparison_limitations, *review.limitations,
        ],
        rejected_evidence_references=rejected,
    )
    report.limitations = list(dict.fromkeys(report.limitations))
    report.rejected_evidence_references = list(dict.fromkeys(report.rejected_evidence_references))
    markdown = render_report(report, evidence)
    save(audit_id, "report.json", report.model_dump(mode="json"))
    save(audit_id, "report.md", markdown)
    save(audit_id, "rejected.json", report.rejected_evidence_references)
    save(audit_id, "status.json", {"audit_id": audit_id, "state": report.state, "stage": "Report saved", "error": None})
    return report, markdown


def render_report(report: AuditReport, evidence: dict) -> str:
    score = report.static_score
    dynamic = report.dynamic_score
    static_text = f"{score.score:g}/10" if score.score is not None else "Not assessed"
    dynamic_text = f"{dynamic.score:g}/10" if dynamic.score is not None else "Not assessed"
    lines = [
        f"# Reproducibility audit: {report.paper.title}", "",
        f"**The static score is {static_text}.** **The dynamic score is {dynamic_text}.**"
        + (f" **The aggregated problem score is {report.aggregated_score.score:g}/10.**" if report.aggregated_score and report.aggregated_score.score is not None else ""), "",
        "Each assessment uses its own 0–10 scale. Both assessments report their coverage separately. Unattempted execution receives an unassessed dynamic score.", "",
        report.summary, "",
        f"The audit assessed {score.criteria_assessed} of {score.criteria_total} static criteria and {dynamic.criteria_assessed} of {dynamic.criteria_total} dynamic criteria. Independent cached-label checks matched {report.claim_coverage['claims_numerically_verified']} of {len(report.paper.claims)} extracted claims.", "",
        f"The audit ID is `{report.audit_id}`.",
        f"The package source is {report.repository.get('source') or 'unresolved'}.",
        f"The audited package revision is `{report.repository.get('revision') or 'unresolved'}`.",
        f"The package SHA-256 is `{report.repository.get('archive_sha256') or 'unresolved'}`.", "",
        "## The paper's reported results", "",
    ]
    for claim in report.paper.claims:
        record = evidence.get(claim.evidence_id)
        location = record.location if record else "unresolved location"
        lines.extend([f"- **{claim.artifact}:** {claim.statement} The reported value is {claim.reported_value}. The source is {claim.evidence_id}, {location}."])
    if report.cached_result_comparisons:
        lines.extend(["", "## Independent counts check the stored results", "", "The following checks count unique solved problem IDs from the authors' cached plausibility labels. The original code generation and validation were not rerun. The mapping from each claim to this calculation comes from model extraction and requires review.", "", "| Claim | Benchmark | Reported | Counted | Comparison | Evidence |", "|---|---|---:|---:|---|---|"])
        for comparison in report.cached_result_comparisons:
            lines.append(f"| {comparison.claim_id} | {comparison.benchmark} | {comparison.reported_value:g} | {comparison.observed_value} | {'Matches' if comparison.matches else 'Differs'} | {comparison.evidence_id} |")
        lines.extend(["", "These independent checks do not award points for executing the authors' code or reproducing the full experiment."])
    lines.extend(["", "## The static score follows a deterministic demo rubric", "", "| Criterion | Awarded | Maximum | Status |", "|---|---:|---:|---|"])
    for component in score.components:
        awarded = "Unresolved" if component.awarded is None else f"{component.awarded:g}"
        lines.append(f"| {component.criterion.replace('_', ' ').capitalize()} | {awarded} | {component.maximum:g} | {component.status.value.replace('_', ' ')} |")
    lines.extend(["", score.rationale, "", "## The dynamic score records execution coverage", "", "| Criterion | Awarded | Maximum | Status |", "|---|---:|---:|---|"])
    for component in dynamic.components:
        awarded = "Unresolved" if component.awarded is None else f"{component.awarded:g}"
        lines.append(f"| {component.criterion.replace('_', ' ').capitalize()} | {awarded} | {component.maximum:g} | {component.status.value.replace('_', ' ')} |")
    lines.extend(["", dynamic.rationale, ""])
    lines.extend(render_dynamic_sections(report))
    lines.extend(["## The static findings require review", ""])
    if not report.findings:
        lines.append("The review produced no findings with source quotes that passed the mechanical evidence check.")
    for index, finding in enumerate(report.findings, 1):
        record = evidence[finding.evidence_id]
        lines.extend([
            f"### {index}. {finding.title} ({finding.severity})", "", finding.explanation, "",
            f"The reproducibility impact is: {finding.reproducibility_impact}", "",
            f"The suggested action is: {finding.remediation}", "",
            f"The source is **{record.evidence_id}**, {record.location}, {record.source_uri}.", "",
            "> " + finding.quote.replace("\n", "\n> "), "",
            "The source quote was verified. " + ("The finding records a deterministic static observation." if finding.basis == "observation" else "The finding is an agent interpretation of that source."), "",
        ])
    lines.extend(["## The proposed reproduction plan remains unexecuted", ""])
    for index, step in enumerate(report.reproduction_plan, 1):
        lines.extend([f"### {index}. {step.target}", "", f"The relevant files are: {', '.join(step.relevant_files)}.", "", f"The prerequisites are: {', '.join(step.prerequisites)}.", "", f"The expected result is: {step.expected_result}", "", f"The supporting evidence IDs are: {', '.join(step.evidence_ids)}.", ""])
        if step.proposed_command:
            lines.extend(["The following command is an unverified proposal.", "", "```bash", step.proposed_command, "```", ""])
    lines.extend(["## The audit has the following limits", ""])
    lines.extend(f"- {limitation}" for limitation in report.limitations)
    if report.rejected_evidence_references:
        lines.extend(["", "## The evidence check rejected these items", ""])
        lines.extend(f"- {message}" for message in report.rejected_evidence_references)
    return "\n".join(lines) + "\n"


def render_dynamic_sections(report: AuditReport) -> list[str]:
    lines = []
    dynamic = report.dynamic_analysis
    if dynamic.get("status") == "completed":
        compile_info, data = dynamic["compile"], dynamic["data"]
        lines.extend(["## Dynamic analysis ran the analysis scripts", "",
                      f"Compilation: {compile_info['python_files']} Python files and {compile_info['notebooks']} notebooks were parsed; {len(compile_info['failures'])} failed. "
                      f"Third-party imports: {', '.join(compile_info['third_party_imports']) or 'none'}. Missing in the audit environment before any install: {', '.join(compile_info['missing_modules']) or 'none'}.", "",
                      f"Stored data: {data['file_count']} data files ({data['total_bytes'] / 1_000_000:.1f} MB) by type {json.dumps(data['by_suffix'])}. Raw data present: {'yes' if data['raw_data_present'] else 'no'}.", ""])
        if dynamic.get("runs"):
            lines.extend(["| Script | Result | Seconds | Files produced | Installed | Evidence |", "|---|---|---:|---|---|---|"])
            for run in dynamic["runs"]:
                outcome = "ran" if run["succeeded"] else ("timed out" if run["timed_out"] else f"failed (exit {run['exit_code']})")
                produced = ", ".join(PurePath(f).name for f in run["produced_files"][:6]) + (" …" if len(run["produced_files"]) > 6 else "")
                lines.append(f"| {run['file']} | {outcome} | {run['seconds']} | {produced or '—'} | {', '.join(run['installed_packages']) or '—'} | {run.get('evidence_id', '')} |")
            lines.append("")
        else:
            lines.extend(["No figure or table scripts were identified for execution.", ""])
        lines.extend([f"The dynamic stage took {dynamic['seconds']} seconds. Inference, training, and generation scripts were deliberately not run.", ""])
    elif dynamic:
        lines.extend(["## Dynamic analysis", "", dynamic.get("reason", "Dynamic analysis was not run."), ""])
    if report.generated_checks:
        lines.extend(["## Generated checks recompute claims from stored data", "", "Each script was written by the model from the data inventory and executed once. A mismatch may be a flaw in the script rather than in the paper.", "",
                      "| Claim | Expected | Computed | Comparison | Script | Evidence |", "|---|---:|---:|---|---|---|"])
        for check in report.generated_checks:
            comparison = "Matches" if check["matches"] else ("Differs" if check["matches"] is False else ("timed out" if check.get("timed_out") else "no RESULT line"))
            lines.append(f"| {check['claim_id']} | {check['expected_value']} | {check['computed_value'] if check['computed_value'] is not None else '—'} | {comparison} | {check['script_path']} | {check.get('evidence_id', '')} |")
        lines.append("")
    references = report.references
    if references:
        counts = references.get("counts", {})
        lines.extend(["## References were checked against public registries", ""])
        if references.get("status") == "not_attempted":
            lines.extend([references.get("reason", "Reference checking was not attempted."), ""])
        else:
            lines.extend([f"{references.get('checked', 0)} of {references.get('total', 0)} references were checked: {counts.get('found', 0)} found, {counts.get('not_found', 0)} not found, {counts.get('title_mismatch', 0)} with a different registered title, {counts.get('unchecked', 0)} unchecked.", ""])
            if not references.get("complete"):
                lines.extend([f"**Reference checking is not fully completed.** {references.get('reason', '')}", ""])
            for item in references.get("unresolved", []):
                lines.append(f"- `{item['key']}` ({item['status'].replace('_', ' ')}): {item['title'][:120]}" + (f" → registry title: {item['matched_title'][:120]}" if item.get("matched_title") else ""))
            if references.get("unresolved"):
                lines.append("")
    if report.correctness_summary or report.problems:
        lines.extend(["## Correctness problems with severity", ""])
        if report.correctness_summary:
            lines.extend([f"Does the code implement what the paper says? **{report.implementation_matches_paper}**. {report.correctness_summary}", ""])
        if report.problems:
            lines.extend(["| # | Problem | Category | Severity | Basis | Evidence |", "|---|---|---|---|---|---|"])
            for index, problem in enumerate(report.problems, 1):
                lines.append(f"| {index} | {problem.title} | {problem.category.replace('_', ' ')} | {problem.severity} | {problem.basis} | {problem.evidence_id} |")
            lines.append("")
            for index, problem in enumerate(report.problems, 1):
                lines.extend([f"**{index}. {problem.title}** ({problem.severity}, {problem.basis}). {problem.rationale}" + (f" Location: {problem.location}." if problem.location else "") + (f" Claims: {', '.join(problem.claim_ids)}." if problem.claim_ids else ""), ""])
        else:
            lines.extend(["No problems were recorded.", ""])
    if report.citation_checks:
        lines.extend(["## Citation support was judged by the model", "", "| Key | Judgement | Rationale |", "|---|---|---|"])
        for check in report.citation_checks:
            lines.append(f"| `{check.key}` | {check.support.replace('_', ' ')} | {check.rationale[:200]} |")
        lines.append("")
    if report.aggregated_score:
        lines.extend(["## Aggregated score", "", f"**{report.aggregated_score.score:g}/10** (provisional). {report.aggregated_score.rationale}", ""])
    return lines

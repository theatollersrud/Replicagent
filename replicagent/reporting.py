import json

from .schemas import AuditReport, AuditFinding, PaperManifest, Review, Status
from .execution_summary import execution_summary
from .comparison import compare_cached_counts
from .scoring import calculate_static_score, calculate_dynamic_score
from .storage import EvidenceLedger, read, save, verify_quote


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


def finalize(audit_id: str, review_value) -> tuple[AuditReport, str]:
    if isinstance(review_value, str):
        review_value = json.loads(review_value)
    review = Review.model_validate(review_value)
    save(audit_id, "review.json", review.model_dump())
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
    # Keep the large index and excerpts in separate stored files.
    repository_summary = {k: v for k, v in repository.items() if k not in {"source_excerpts", "file_paths"}}
    try:
        execution = read(audit_id, "execution.json")
    except FileNotFoundError:
        execution = None
    report = AuditReport(
        audit_id=audit_id, state="completed_audit" if execution else "completed_static_audit", paper=manifest,
        repository=repository_summary, findings=findings, reproduction_plan=plan,
        static_score=calculate_static_score(repository), dynamic_score=calculate_dynamic_score(execution),
        dynamic_status=Status(execution["status"]) if execution else Status.NOT_ATTEMPTED,
        executions=[execution] if execution else [],
        summary=(f"The audit inspected {repository.get('file_count', 0)} artifact files and retained {len(findings)} findings with checked source references. "
                 f"It completed independent comparisons for {len(comparisons)} extracted claims using author-provided cached labels. "
                 "The findings below separate deterministic observations from agent interpretations. Full experimental reproduction remains untested."),
        claim_coverage={"claims_extracted": len(manifest.claims), "claims_numerically_verified": sum(c.matches for c in comparisons), "claims_compared_against_cached_labels": len(comparisons), "fraction_numerically_verified": sum(c.matches for c in comparisons) / len(manifest.claims) if manifest.claims else None, "scope": "Independent checks of stored labels. Dynamic execution is reported separately and does not by itself verify a paper claim."},
        cached_result_comparisons=comparisons,
        limitations=[
            "Dynamic testing covers the attempted commands within a five-minute cloud execution budget. A successful command establishes that the selected code ran in this environment. Verifying a paper result requires a separate comparison against that result." if execution else "Repository code was not executed. Independent checks of cached labels, when available, do not establish full experimental reproduction.",
            "Source quotes and evidence IDs were checked mechanically. The reasoning connecting a quote to an interpretation still requires review.",
            "The scoring rubric checks coarse artifact and documentation indicators. It has not been calibrated against expert assessments.",
            "Mathematical verification and comprehensive reference verification remain unimplemented.",
            "Discovered artifact versions may postdate the audited paper. Their correspondence to the exact paper version requires confirmation.",
            *repository.get("limitations", []), *comparison_limitations,
            *[("The review before execution recorded this limitation: " + item) if execution else item for item in review.limitations],
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
        f"**The static score is {static_text}.** **The dynamic score is {dynamic_text}.**", "",
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
    lines.extend(["", "Execution and verification of the paper's numerical results are assessed separately."])
    for execution in report.executions:
        lines.extend(render_execution(execution))
    lines.extend(["", "## The static findings require review", ""])
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
    lines.extend(["## The static review proposed the following reproduction plan", "",
                  "The dynamic summary above reports observed execution. This plan records the proposals made before execution.", ""])
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


def render_execution(execution: dict) -> list[str]:
    """Keep the final report concise. Complete observations remain in JSON."""
    return execution_summary(execution)

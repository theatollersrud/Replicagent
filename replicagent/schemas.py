"""Small shared contracts for the demo. Model conclusions are distinct from observations."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Status(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    BLOCKED = "blocked"
    FAILED = "failed"
    PARTIAL = "partially_verified"
    VERIFIED = "verified"


class Evidence(BaseModel):
    schema_version: str = "1.0"
    evidence_id: str
    audit_id: str
    source_type: str
    source_uri: str
    location: str
    content_hash: str
    observation: str
    verification_method: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    related_claim_ids: list[str] = Field(default_factory=list)


class ScientificClaim(BaseModel):
    claim_id: str
    artifact: str = Field(description="The reported table, figure, or experimental result.")
    statement: str
    reported_value: str
    evidence_id: str
    quote: str = Field(description="A short exact quote from the supplied source evidence.")


class PaperManifest(BaseModel):
    title: str
    authors: list[str]
    contribution: str
    claims: list[ScientificClaim]
    datasets: list[str]
    artifact_urls: list[str]
    compute_requirements: list[str]
    limitations: list[str]


class AuditFinding(BaseModel):
    title: str
    severity: Literal["critical", "high", "medium", "low", "informational"]
    explanation: str
    evidence_id: str
    quote: str
    claim_ids: list[str]
    reproducibility_impact: str
    remediation: str
    basis: Literal["observation", "interpretation"] = "interpretation"
    verification_status: str = "source_quote_verified"


class PlanStep(BaseModel):
    target: str
    relevant_files: list[str]
    proposed_command: str
    prerequisites: list[str]
    expected_result: str
    evidence_ids: list[str]


class Review(BaseModel):
    summary: str
    findings: list[AuditFinding]
    reproduction_plan: list[PlanStep]
    limitations: list[str]


class ScoreComponent(BaseModel):
    criterion: str
    maximum: float
    awarded: float | None
    status: Status
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


class ResultComparison(BaseModel):
    claim_id: str
    benchmark: str
    reported_value: float
    observed_value: int
    matches: bool
    evidence_id: str
    method: str = "Independent counting of unique solved problem IDs from author-provided cached plausibility labels."
    limitation: str = "The claim mapping is model-extracted. The original generation and validation were not rerun."


class ScoreBreakdown(BaseModel):
    score: float | None
    possible_after_untested_checks: float
    maximum: float = 10.0
    provisional: bool = True
    status: Status = Status.PARTIAL
    criteria_assessed: int
    criteria_total: int
    components: list[ScoreComponent]
    rationale: str


class AuditRequest(BaseModel):
    paper_source: str = "https://arxiv.org/abs/2510.21513"
    repository_url: str | None = None
    mock: bool = False


class AuditReport(BaseModel):
    schema_version: str = "2.0"
    audit_id: str
    state: str
    paper: PaperManifest
    repository: dict
    findings: list[AuditFinding]
    reproduction_plan: list[PlanStep]
    static_score: ScoreBreakdown
    dynamic_score: ScoreBreakdown
    dynamic_status: Status = Status.NOT_ATTEMPTED
    scientific_correctness_status: Status = Status.NOT_ATTEMPTED
    reference_verification_status: Status = Status.NOT_ATTEMPTED
    claim_coverage: dict
    cached_result_comparisons: list[ResultComparison] = Field(default_factory=list)
    summary: str
    limitations: list[str]
    rejected_evidence_references: list[str]
    dynamic_analysis: dict = Field(default_factory=dict)
    references: dict = Field(default_factory=dict)
    generated_checks: list[dict] = Field(default_factory=list)
    check_loop: dict = Field(default_factory=dict)
    metric_trace: list[dict] = Field(default_factory=list)
    problems: list["Problem"] = Field(default_factory=list)
    citation_checks: list["CitationCheck"] = Field(default_factory=list)
    correctness_summary: str = ""
    implementation_matches_paper: str = "unknown"
    aggregated_score: "AggregatedScore | None" = None


class GeneratedCheck(BaseModel):
    claim_id: str = Field(description="The claim ID for a claim check, or a short label such as K01 for a consistency check.")
    kind: Literal["claim", "consistency"] = "claim"
    description: str = Field(description="What the script computes and from which files, including every filter applied.")
    script: str = Field(description="A self-contained Python 3 script that prints RESULT=<value> as its last line.")
    expected_value: str = Field(default="", description="The paper's reported value, or a JSON object of cell label to value for a table sweep. Empty for consistency checks.")


class CheckPlan(BaseModel):
    checks: list[GeneratedCheck]
    notes: list[str]


class CheckRevision(BaseModel):
    claim_id: str
    script: str = Field(default="", description="The corrected script, or empty when giving up.")
    give_up: bool = False
    reason: str = Field(default="", description="Why the previous attempt failed, or why the value cannot be computed from the stored data.")


class ConsistencyAssessment(BaseModel):
    check_id: str
    issue: bool = Field(description="True when the output reveals an inconsistency, missing data, or provenance problem.")
    severity: Literal["critical", "high", "medium", "low", "informational"] = "informational"
    title: str = ""
    explanation: str = ""


class RevisionPlan(BaseModel):
    revisions: list[CheckRevision]
    assessments: list[ConsistencyAssessment]
    notes: list[str]


class MetricTrace(BaseModel):
    name: str = Field(description="The metric, selection strategy, filter, normalization, or panel definition as the paper names it.")
    paper_definition: str
    paper_evidence_id: str
    paper_quote: str = Field(description="A short exact quote from the paper source that defines it.")
    code_file: str = Field(default="", description="The implementing file, exactly as listed, or empty when not found.")
    code_evidence_id: str = ""
    code_quote: str = Field(default="", description="A short exact quote from that code record.")
    code_definition: str = ""
    column_selection: str = Field(default="", description="The exact expression that selects columns or files, and EVERY column or file name the code creates or reads that it matches. 'n/a' when none.")
    panel: str = Field(default="", description="Which models' values the code iterates, sums, averages, or selects over, versus the set the paper names for this computation.")
    missing_value_policy: str = Field(default="", description="What the code does with missing, NaN, or absent entries (for example pandas sum skips NaN), and whether the paper states a policy.")
    normalization_scope: str = Field(default="", description="What the numerator counts (which tokens or items, prompt included or not) and what the denominator counts (bytes or tokens of what), versus the paper.")
    consistent: Literal["yes", "partially", "no", "not_found"]
    discrepancy: str = Field(default="", description="How the code differs from the paper, if it does.")


class MetricMap(BaseModel):
    metrics: list[MetricTrace]
    limitations: list[str]


class Problem(BaseModel):
    title: str
    category: Literal["implementation_discrepancy", "mathematical_error", "code_bug", "execution_failure", "missing_data", "result_mismatch", "reference_missing", "citation_misuse", "documentation"]
    severity: Literal["critical", "high", "medium", "low", "informational"]
    rationale: str
    evidence_id: str
    quote: str = Field(description="A short exact quote from the cited evidence record.")
    claim_ids: list[str] = Field(default_factory=list)
    location: str = ""
    basis: Literal["observation", "interpretation"] = "interpretation"


class CitationCheck(BaseModel):
    key: str
    statement: str = Field(description="The sentence in the paper that cites this reference.")
    support: Literal["supports", "partially_supports", "does_not_support", "unknown"]
    rationale: str


class CorrectnessReview(BaseModel):
    summary: str
    implementation_matches_paper: Literal["yes", "partially", "no", "unknown"]
    implementation_rationale: str
    problems: list[Problem]
    citation_checks: list[CitationCheck]
    limitations: list[str]


class AggregatedScore(BaseModel):
    score: float | None
    maximum: float = 10.0
    provisional: bool = True
    penalties: list[dict]
    rationale: str

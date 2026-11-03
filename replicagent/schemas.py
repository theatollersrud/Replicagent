"""Small shared contracts for the demo. Model conclusions are distinct from observations."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    paper_source: str | None = None
    repository_url: str | None = None
    mock: bool = False

    @model_validator(mode="after")
    def require_paper_for_live_audit(self):
        if not self.mock and not (self.paper_source and self.paper_source.strip()):
            raise ValueError("A paper_source is required for a live audit.")
        return self


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
    executions: list[dict] = Field(default_factory=list)
    summary: str
    limitations: list[str]
    rejected_evidence_references: list[str]

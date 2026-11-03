"""A configurable, deterministic demonstration rubric, without LLM-assigned points."""

import json
from pathlib import Path

from .schemas import ScoreBreakdown, ScoreComponent, Status

POLICY_PATH = Path(__file__).resolve().parent.parent / "configs" / "scoring.json"


def calculate_static_score(repository: dict, policy: dict | None = None) -> ScoreBreakdown:
    policy = policy or json.loads(POLICY_PATH.read_text())
    weights = policy["static_weights"]
    if any(v < 0 for v in weights.values()) or abs(sum(weights.values()) - 10) > 1e-8:
        raise ValueError("Scoring weights must be non-negative and sum to 10.")
    facts = repository.get("facts", {})
    components = []
    for criterion, maximum in weights.items():
        assessed = criterion in facts and repository.get("status") == "inspected"
        if assessed:
            observed = bool(facts[criterion])
            awarded = maximum if observed else 0.0
            rationale = (
                f"The static presence check for {criterion.replace('_', ' ')} matched the inspected package."
                if observed else f"The static presence check for {criterion.replace('_', ' ')} did not match the inspected package."
            )
            status = Status.PARTIAL
        else:
            awarded = None
            rationale = "This criterion has not been tested. Its points remain unresolved."
            status = Status.NOT_ATTEMPTED
        components.append(ScoreComponent(criterion=criterion, maximum=maximum, awarded=awarded, status=status, rationale=rationale, evidence_ids=[repository["static_checks_evidence_id"]] if assessed and repository.get("static_checks_evidence_id") else []))
    earned = round(sum(c.awarded or 0 for c in components), 2)
    unresolved = sum(c.maximum for c in components if c.awarded is None)
    capped = repository.get("status") == "inspected" and facts.get("implementation") is False
    cap = policy["no_code_cap"] if capped else 10
    return ScoreBreakdown(
        score=min(earned, cap) if any(c.awarded is not None for c in components) else None,
        possible_after_untested_checks=min(round(earned + unresolved, 2), cap),
        status=Status.PARTIAL if any(c.awarded is not None for c in components) else Status.BLOCKED,
        criteria_assessed=sum(c.awarded is not None for c in components), criteria_total=len(components),
        components=components,
        rationale=(
            "The static score measures artifact availability, documentation, and environment indicators on its own 0–10 scale. "
            "It uses a coarse, uncalibrated demo rubric. "
            "Untested criteria retain an explicit unresolved allowance, rather than counting as demonstrated failures. "
            "File and command presence does not establish experimental completeness or successful reproduction."
            + (" The inspected package contains no supported implementation files, so the provisional score is capped at 1/10. Other artifact sources remain outside this check." if capped else "")
        ),
    )


def calculate_dynamic_score(execution: dict | None = None, policy: dict | None = None) -> ScoreBreakdown:
    policy = policy or json.loads(POLICY_PATH.read_text())
    weights = policy["dynamic_weights"]
    if any(v < 0 for v in weights.values()) or abs(sum(weights.values()) - 10) > 1e-8:
        raise ValueError("Dynamic scoring weights must be non-negative and sum to 10.")
    execution = execution or {}
    components = []
    for criterion, maximum in weights.items():
        result = execution.get("criteria", {}).get(criterion)
        assessed = result is not None and result.get("fraction") is not None
        fraction = result["fraction"] if assessed else None
        if assessed and not 0 <= fraction <= 1:
            raise ValueError("Dynamic criterion fractions must be between zero and one.")
        components.append(ScoreComponent(
            criterion=criterion, maximum=maximum,
            awarded=round(maximum * fraction, 2) if assessed else None,
            status=Status(result["status"]) if assessed else Status(execution.get("status", "not_attempted")),
            rationale=result["rationale"] if assessed else execution.get("reason", "This dynamic criterion has not been tested."),
            evidence_ids=result.get("evidence_ids", []) if assessed else [],
        ))
    assessed = sum(c.awarded is not None for c in components)
    earned = round(sum(c.awarded or 0 for c in components), 2)
    unresolved = sum(c.maximum for c in components if c.awarded is None)
    return ScoreBreakdown(
        score=earned if assessed else None, possible_after_untested_checks=round(earned + unresolved, 2),
        criteria_assessed=assessed, criteria_total=len(components), components=components,
        status=Status(execution.get("status", "not_attempted")),
        rationale="The dynamic score has its own 0–10 scale. Executability contributes up to 4 points and reproduction of reported results contributes up to 6 points. Unattempted checks remain unresolved. Successful smoke tests alone do not establish reproduction of the paper's results.",
    )

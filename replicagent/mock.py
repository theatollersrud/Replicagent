"""An explicitly labelled fixture workflow for offline tests and demonstrations."""

import json
import zipfile

from .reporting import finalize
from .services import inspect_archive
from .storage import EvidenceLedger, audit_dir, save


def run_mock(audit_id: str):
    ledger = EvidenceLedger(audit_id)
    quote = "The synthetic experiment reports an accuracy of 0.75 on four examples."
    record = ledger.add("mock_paper", "fixture://synthetic-paper", "page 1", quote, "Read a synthetic fixture. This is not a real paper audit.")
    manifest = {
        "title": "Synthetic offline audit fixture", "authors": ["Fixture author"],
        "contribution": "This fixture tests the audit plumbing without model or network calls.",
        "claims": [{"claim_id": "C01", "artifact": "Synthetic Table 1", "statement": "The fixture reports an accuracy of 0.75.", "reported_value": "0.75", "evidence_id": record.evidence_id, "quote": quote}],
        "datasets": ["Four synthetic examples"], "artifact_urls": [], "compute_requirements": [], "limitations": ["This is a synthetic fixture."],
    }
    archive = audit_dir(audit_id) / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("fixture/README.md", "# Synthetic fixture\nInstall with pip install -r requirements.txt\nRun python evaluate.py\n")
        target.writestr("fixture/evaluate.py", "def accuracy():\n    return 3 / 4\n")
        target.writestr("fixture/requirements.txt", "numpy==2.0.0\n")
    repository = inspect_archive(audit_id, archive, "fixture://synthetic-package", "fixture-v1")
    save(audit_id, "manifest.json", manifest)
    save(audit_id, "repository.json", repository)
    save(audit_id, "rejected.json", [])
    review = {
        "summary": "This offline fixture verifies the report pipeline. It makes no claim about a real research paper.",
        "findings": [], "reproduction_plan": [], "limitations": ["The entire audit uses synthetic inputs and makes no model calls."],
    }
    return finalize(audit_id, review)

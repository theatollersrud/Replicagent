"""Local persistence keeps full text and logs outside the agent session."""

import hashlib
import json
import os
import re
from pathlib import Path

from .schemas import Evidence

ROOT = Path(__file__).resolve().parent.parent
AUDITS = Path(os.getenv("AUDITOR_DATA_DIR", str(ROOT / "audits")))


def audit_dir(audit_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", audit_id):
        raise ValueError("The audit ID is invalid.")
    path = AUDITS / audit_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def save(audit_id: str, name: str, value):
    path = audit_dir(audit_id) / name
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def read(audit_id: str, name: str):
    return json.loads((audit_dir(audit_id) / name).read_text(encoding="utf-8"))


class EvidenceLedger:
    def __init__(self, audit_id: str):
        self.audit_id = audit_id
        self.path = audit_dir(audit_id) / "evidence.jsonl"

    def records(self) -> list[Evidence]:
        if not self.path.exists():
            return []
        return [Evidence.model_validate_json(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def add(self, source_type: str, source_uri: str, location: str, observation: str, method: str) -> Evidence:
        records = self.records()
        record = Evidence(
            evidence_id=f"E{len(records) + 1:04d}", audit_id=self.audit_id,
            source_type=source_type, source_uri=source_uri, location=location,
            content_hash=hashlib.sha256(observation.encode()).hexdigest(),
            observation=observation, verification_method=method,
        )
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(record.model_dump_json() + "\n")
        return record


def verify_quote(evidence: dict, evidence_id: str, quote: str) -> bool:
    if evidence_id not in evidence or len(quote.strip()) < 8:
        return False
    record = evidence[evidence_id]
    if hashlib.sha256(record.observation.encode()).hexdigest() != record.content_hash:
        return False
    normalise = lambda s: " ".join(s.split()).casefold()
    return normalise(quote) in normalise(evidence[evidence_id].observation)

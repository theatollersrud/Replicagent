from types import SimpleNamespace

import httpx
import pytest

from replicagent import services


def test_zenodo_doi_and_github_profile_normalization():
    assert services.normalize_artifact_url("https://doi.org/10.5281/zenodo.15294695") == "https://zenodo.org/records/15294695"
    assert services.normalize_artifact_url("https://github.com/prebenness/") is None
    assert services.normalize_artifact_url("https://github.com/prebenness/causal_disentanglement_robustness") == "https://github.com/prebenness/causal_disentanglement_robustness"


def test_github_revision_uses_git_without_api(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "public_url", lambda url: None)
    monkeypatch.setattr(services.subprocess, "run", lambda args, **kwargs: (calls.append(args) or SimpleNamespace(returncode=0, stdout="a" * 40 + "\tHEAD\n", stderr="")))
    monkeypatch.setattr(services, "get_json", lambda *args, **kwargs: pytest.fail("The GitHub API must not be called."))
    url, revision, _ = services.get_archive("https://github.com/prebenness/causal_disentanglement_robustness")
    assert revision == "a" * 40
    assert revision in url
    assert calls[0][:3] == ["git", "ls-remote", "--exit-code"]


def test_metadata_redirect_is_validated_and_resolved(monkeypatch):
    checked = []
    monkeypatch.setattr(services, "public_url", checked.append)
    first = "https://zenodo.org/api/records/15294695"
    final = "https://zenodo.org/api/records/15294696"
    responses = iter([httpx.Response(302, headers={"location": "/api/records/15294696"}, request=httpx.Request("GET", first)), httpx.Response(200, json={"id": 15294696}, request=httpx.Request("GET", final))])
    monkeypatch.setattr(services.httpx, "get", lambda *args, **kwargs: next(responses))
    assert services.get_json(first) == {"id": 15294696}
    assert checked == [first, final]


def test_discovery_keeps_zenodo_doi_and_records_resolved_version(monkeypatch, tmp_path):
    monkeypatch.setattr(services, "ROOT", tmp_path)
    monkeypatch.setattr(services, "get_json", lambda *args, **kwargs: {"id": 15294696, "files": [{"key": "package.zip", "size": 100, "links": {"self": "https://zenodo.org/package.zip"}}]})
    url, revision, _ = services.get_archive("https://doi.org/10.5281/zenodo.15294695")
    assert revision == "zenodo:15294696"
    observed = []
    monkeypatch.setattr(services, "download", lambda *args: None)
    monkeypatch.setattr(services, "inspect_archive", lambda audit_id, path, source, revision: (observed.append(source) or {"status": "inspected"}))
    monkeypatch.setattr(services, "EvidenceLedger", lambda audit_id: SimpleNamespace(add=lambda *args: None))
    result = services.inspect_repository("fixture", {"artifact_urls": ["https://doi.org/10.5281/zenodo.15294695"]}, {"links": [], "source": "uploaded-paper.pdf"})
    assert result["status"] == "inspected"
    assert observed == ["https://zenodo.org/records/15294695"]


def test_download_retries_transient_server_failure(monkeypatch, tmp_path):
    attempts = []
    def fetch(url, target, limit):
        attempts.append(url)
        if len(attempts) == 1:
            response = httpx.Response(500, request=httpx.Request("GET", url))
            response.raise_for_status()
        target.write_bytes(b"archive")
    monkeypatch.setattr(services, "_download_once", fetch)
    monkeypatch.setattr(services.time, "sleep", lambda delay: None)
    target = tmp_path / "archive.zip"
    services.download("https://example.com/archive.zip", target)
    assert len(attempts) == 2
    assert target.read_bytes() == b"archive"

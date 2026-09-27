"""Smoke tests for the additive CLI extension commands.

All commands run against a tmp-dir store via ``--data``. Fictitious data
only; the engine is stubbed except where the command logic itself is the
subject (error paths).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aafc_engine import cli


@pytest.fixture()
def argv_data(tmp_path):
    """Global ``--data DIR`` argv prefix pointing at a tmp store."""
    return ["--data", str(tmp_path / "cli-data")]


def _run(capsys, argv):
    """Run cli.main; return (exit_code, stdout, stderr)."""
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _register(capsys, argv_data, email="cli@example.test", name="CLI User"):
    code, out, _ = _run(
        capsys,
        [*argv_data, "customer", "register", "--email", email, "--name", name],
    )
    assert code == 0
    return out


def test_customer_resolve_new(capsys, argv_data):
    """`customer resolve` on an unknown email reports NEW + what is needed."""
    code, out, _ = _run(
        capsys, [*argv_data, "customer", "resolve", "--email", "new@example.test"]
    )
    assert code == 0
    assert "NEW" in out
    assert "name" in out.lower()


def test_customer_register_then_resolve_found(capsys, argv_data):
    """`customer register` then `customer resolve` reports FOUND."""
    _register(capsys, argv_data)
    code, out, _ = _run(
        capsys, [*argv_data, "customer", "resolve", "--email", "cli@example.test"]
    )
    assert code == 0
    assert "FOUND" in out
    assert "CLI User" in out


def test_customer_file_counts(capsys, argv_data):
    """`customer file` prints a readable cabinet summary with counts."""
    _register(capsys, argv_data)
    code, out, _ = _run(
        capsys, [*argv_data, "customer", "file", "--email", "cli@example.test"]
    )
    assert code == 0
    for section in ("websites", "audits", "findings", "opportunities",
                    "money", "history"):
        assert section in out
    code, out, _ = _run(
        capsys,
        [*argv_data, "--json", "customer", "file", "--email", "cli@example.test"],
    )
    assert code == 0
    cabinet = json.loads(out)["customer"]
    assert cabinet["identity"]["email"] == "cli@example.test"


def test_footprint_table(capsys, argv_data, monkeypatch):
    """`footprint` prints the platform/status/confidence/evidence table."""
    _register(capsys, argv_data)
    from aafc_engine import clients, websites

    monkeypatch.setattr(
        "aafc_engine.footprint._fetch_homepage",
        lambda url: {"ok": True, "status": 200, "error": None,
                     "text": '<html><a href="https://instagram.com/acme">ig</a></html>'},
    )
    from aafc_engine.store import Store

    store = Store(argv_data[1])
    client = clients.find_client_by_email(store, "cli@example.test")
    websites.add_website(store, client["id"], "https://example.com", authorized=True)
    code, out, _ = _run(
        capsys, [*argv_data, "footprint", "--email", "cli@example.test"]
    )
    assert code == 0
    assert "instagram" in out
    assert "PUBLICLY VERIFIED" in out
    assert "NOT FOUND" in out


def test_audit_full_refuses_unauthorized(capsys, argv_data):
    """`audit-full` on an unauthorized website: ERROR to stderr, exit 1."""
    _register(capsys, argv_data)
    from aafc_engine import clients, websites
    from aafc_engine.store import Store

    store = Store(argv_data[1])
    client = clients.find_client_by_email(store, "cli@example.test")
    websites.add_website(store, client["id"], "https://example.com")  # not authorized
    code, out, err = _run(
        capsys, [*argv_data, "audit-full", "--email", "cli@example.test"]
    )
    assert code == 1
    assert "ERROR" in err


def test_audit_full_unknown_email(capsys, argv_data):
    """`audit-full` on an unknown email: ERROR to stderr, exit 1."""
    code, out, err = _run(
        capsys, [*argv_data, "audit-full", "--email", "nobody@example.test"]
    )
    assert code == 1
    assert "ERROR" in err


def test_opportunities_command(capsys, argv_data, monkeypatch):
    """`opportunities` prints service/problem/evidence/pricing/status lines."""
    _register(capsys, argv_data)
    from aafc_engine import clients, websites
    from aafc_engine.store import Store

    store = Store(argv_data[1])
    client = clients.find_client_by_email(store, "cli@example.test")
    site, _ = websites.add_website(
        store, client["id"], "https://example.com", authorized=True
    )

    def fake_audit_site(url, max_pages=6, **kwargs):
        return {
            "url": url, "pages_crawled": 1, "score": 70, "grade": "C",
            "counts": {"critical": 0, "warning": 1, "info": 0},
            "findings": [{
                "page": url, "check": "meta_description", "severity": "warning",
                "title": "Missing meta description",
                "evidence": "No <meta name='description'> found.",
                "why_it_matters": "why", "recommended_fix": "fix", "technical": "",
            }],
            "elapsed_total": 0.01, "notes": ["stubbed"],
        }

    monkeypatch.setattr(
        "aafc_engine.auditor.registry.engine.audit_site", fake_audit_site
    )
    from aafc_engine.auditor import registry as audit_registry

    audit_registry.run_audit(store, client["id"], site["id"], max_pages=1)

    code, out, _ = _run(
        capsys,
        [*argv_data, "opportunities", "--email", "cli@example.test",
         "--current-offering", "website-design-build"],
    )
    assert code == 0
    assert "seo" in out
    assert "problem:" in out
    assert "evidence:" in out
    assert "pricing=" in out

    code, out, _ = _run(
        capsys,
        [*argv_data, "--json", "opportunity-map", "--email", "cli@example.test",
         "--current-offering", "website-design-build"],
    )
    assert code == 0
    path = Path(json.loads(out)["path"])
    assert path.is_file()
    md = path.read_text(encoding="utf-8")
    assert "## Current offering" in md
    assert "## Opportunities (evidence-supported only)" in md
    assert "## Expansions not evidence-supported" in md
    assert "no padded items" in md or "not recommended" in md

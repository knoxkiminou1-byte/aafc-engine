"""AAFC service catalog (grounded in reality only).

The catalog lives in ``aafc_engine/data/services.yaml``. Every
``evidence_triggers`` entry is validated at load time against the REAL
check names: the auditor engine's check names (verified against
``aafc_engine/auditor/engine.py``) plus the crosscheck module's
``XCHECK_NAMES``. An unknown trigger raises ``ValueError`` -- the catalog
can never silently reference a check that does not exist.

``match_services()`` matches a service ONLY when at least one finding
with result FAIL or ERROR has a check name in the service's
``evidence_triggers``. PASS findings are never a basis for a
recommendation (a passing check is evidence that no problem exists).
Services with an empty trigger list are honest about being
undetectable from a public audit and are never evidence-matched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import crosscheck

#: Real auditor engine check names (verified against engine.py's checks).
ENGINE_CHECKS = frozenset({
    "https", "hsts", "indexability", "title", "meta_description",
    "viewport", "charset", "h1", "heading_order", "img_alt",
    "canonical", "open_graph", "structured_data", "lang",
    "mixed_content", "forms", "cta", "page_weight", "ttfb",
    "robots_txt", "sitemap", "broken_links", "fetch", "http_status",
    "parse",
})

#: Check names a service's evidence_triggers may reference.
KNOWN_CHECKS = ENGINE_CHECKS | frozenset(crosscheck.XCHECK_NAMES)

_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "services.yaml"

_catalog: list[dict[str, Any]] | None = None


def load_catalog() -> list[dict[str, Any]]:
    """Load and validate the service catalog from services.yaml.

    Returns:
        List of service dicts in file order.

    Raises:
        ValueError: If a service is missing required fields or references
            an unknown check name in ``evidence_triggers``.
        FileNotFoundError: If the catalog file is missing.
    """
    global _catalog
    if _catalog is not None:
        return _catalog
    with open(_CATALOG_PATH, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, list) or not raw:
        raise ValueError("service catalog must be a non-empty YAML list")
    required = {
        "slug", "name", "tagline", "covers", "evidence_triggers",
        "implementation_path", "pricing_model",
    }
    seen: set[str] = set()
    for entry in raw:
        missing = required - set(entry)
        if missing:
            raise ValueError(
                f"service entry missing fields {sorted(missing)}: {entry!r}"
            )
        slug = entry["slug"]
        if slug in seen:
            raise ValueError(f"duplicate service slug: {slug!r}")
        seen.add(slug)
        if entry["pricing_model"] not in ("one-time", "recurring"):
            raise ValueError(
                f"service {slug!r}: pricing_model must be 'one-time' or "
                f"'recurring', got {entry['pricing_model']!r}"
            )
        unknown = [
            t for t in entry["evidence_triggers"] if t not in KNOWN_CHECKS
        ]
        if unknown:
            raise ValueError(
                f"service {slug!r}: unknown evidence trigger(s) {unknown}; "
                "triggers must be real auditor engine or crosscheck check names"
            )
    _catalog = raw
    return _catalog


def list_services() -> list[dict[str, Any]]:
    """Return every service in the catalog.

    Returns:
        List of service dicts in catalog order.
    """
    return list(load_catalog())


def get_service(slug: str) -> dict[str, Any]:
    """Return one service by slug.

    Args:
        slug: Service slug (e.g. ``"seo"``).

    Returns:
        The service dict.

    Raises:
        KeyError: If no service has that slug.
    """
    for service in load_catalog():
        if service["slug"] == slug:
            return service
    raise KeyError(f"unknown service: {slug!r}")


def match_services(findings: list[dict]) -> list[dict]:
    """Match services to findings, evidence-gated.

    A service matches ONLY if at least one finding with result FAIL or
    ERROR has a check name in the service's ``evidence_triggers``. PASS
    findings are ignored (a passing check is evidence of no problem), and
    services with empty trigger lists never match.

    Args:
        findings: List of standard-schema finding dicts.

    Returns:
        List of ``{"service": slug, "triggered_by": [finding ids],
        "why": str}`` in catalog order.
    """
    actionable = [
        f for f in findings if f.get("result") in ("FAIL", "ERROR")
    ]
    matches: list[dict] = []
    for service in load_catalog():
        triggers = set(service.get("evidence_triggers", []))
        if not triggers:
            continue
        hit = [
            f for f in actionable if f.get("check") in triggers
        ]
        if not hit:
            continue
        check_names = sorted({str(f.get("check")) for f in hit})
        matches.append(
            {
                "service": service["slug"],
                "triggered_by": [f.get("id") for f in hit],
                "why": (
                    f"{len(hit)} finding(s) match this service's evidence "
                    f"triggers: {', '.join(check_names)}"
                ),
            }
        )
    return matches

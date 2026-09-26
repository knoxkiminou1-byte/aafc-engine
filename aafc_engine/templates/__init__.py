"""Email templates for AAFC client communications.

Templates live as ``templates/<name>.txt`` files with ``{{variable}}``
placeholders and no hard-coded customer data.
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = [
    "audit_received",
    "audit_complete",
    "report_ready",
    "fix_plan_ready",
    "project_ready",
    "fixes_completed",
    "reaudit_complete",
]

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_TEMPLATE_DIR = Path(__file__).resolve().parent


def render(name: str, variables: dict) -> str:
    """Render the template ``name`` with ``variables`` substituted.

    Replaces every ``{{var}}`` placeholder with ``str(variables["var"])``.

    Raises:
        KeyError: if ``name`` is not a known template.
        ValueError: listing every placeholder missing from ``variables``.
    """
    if name not in TEMPLATES:
        raise KeyError(f"unknown template: {name!r} (known: {', '.join(TEMPLATES)})")
    text = (_TEMPLATE_DIR / f"{name}.txt").read_text(encoding="utf-8")
    missing = sorted({v for v in _VAR_RE.findall(text) if v not in variables})
    if missing:
        raise ValueError(
            f"missing variables for template {name!r}: {', '.join(missing)}"
        )
    return _VAR_RE.sub(lambda m: str(variables[m.group(1)]), text)

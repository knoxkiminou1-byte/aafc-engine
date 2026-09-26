"""AAFC Engine auditor package.

The single audit implementation lives in :mod:`aafc_engine.auditor.engine`
(vendored from sitepulse); the plugin registry lives in
:mod:`aafc_engine.auditor.registry`.
"""

from aafc_engine.auditor.engine import audit_site

__all__ = ["audit_site"]

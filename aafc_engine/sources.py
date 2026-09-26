"""Customer source/origin attribution (no guessing).

A customer's source is a fact about how they arrived, not something to
infer. It is set once, explicitly, and is PERMANENT: changing it after a
real (non-UNKNOWN) source has been recorded raises ``ValueError`` unless
``force=True`` is passed. Every change is logged to history.
"""

from __future__ import annotations

from typing import Any

from . import customers
from .clients import get_client
from .store import Store, utc_now_iso

SOURCES = [
    "referral",
    "website",
    "email",
    "social",
    "campaign",
    "outreach",
    "existing-customer",
    "direct",
    "other",
    "UNKNOWN",
]

_SOURCE_FILE = "sources.json"


def _default_source() -> dict:
    """Return the default (unset) source record."""
    return {"source": "UNKNOWN", "detail": "", "set_at": None}


def get_source(store: Store, client_id: str) -> dict:
    """Return a customer's source record.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        ``{"source", "detail", "set_at"}``; ``{"source": "UNKNOWN",
        "detail": "", "set_at": None}`` when never set.
    """
    get_client(store, client_id)  # raises if unknown
    record = store.read_json(client_id, _SOURCE_FILE, default=None)
    if not isinstance(record, dict):
        return _default_source()
    return {
        "source": record.get("source", "UNKNOWN"),
        "detail": record.get("detail", ""),
        "set_at": record.get("set_at"),
    }


def set_source(
    store: Store,
    client_id: str,
    source: str,
    detail: str = "",
    force: bool = False,
) -> dict:
    """Set a customer's source. Permanent once set to a real value.

    Args:
        store: The Store.
        client_id: The client identifier.
        source: Must be one of :data:`SOURCES`.
        detail: Optional detail (who referred them, which campaign, ...).
        force: When True, allow overwriting an already-set non-UNKNOWN
            source (the override is still logged to history).

    Returns:
        The saved source record ``{"source", "detail", "set_at"}``.

    Raises:
        ValueError: If ``source`` is not in :data:`SOURCES`, or if a
            non-UNKNOWN source already exists and differs from ``source``
            without ``force=True``.
    """
    if source not in SOURCES:
        raise ValueError(
            f"unknown source {source!r}; must be one of {SOURCES}"
        )
    current = get_source(store, client_id)
    if (
        current["source"] != "UNKNOWN"
        and current["source"] != source
        and not force
    ):
        raise ValueError(
            f"source is permanent once set: existing={current['source']!r}, "
            f"refusing to overwrite with {source!r} (use force=True to override)"
        )
    record = {"source": source, "detail": detail, "set_at": utc_now_iso()}
    store.write_json(client_id, record, _SOURCE_FILE)
    customers.log_history(
        store,
        client_id,
        "source_set",
        f"source={source!r} detail={detail!r}"
        + (" (forced override)" if force else ""),
    )
    return record

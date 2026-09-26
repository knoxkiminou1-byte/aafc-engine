"""Deadline tracking per client, sorted by due date."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .store import Store, utc_now_iso

_DEADLINES_FILE = "deadlines.json"

#: Strict YYYY-MM-DD shape check before datetime parsing (strptime alone
#: accepts e.g. "2026-1-5", which is not YYYY-MM-DD).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_due_date(due_date: str) -> str:
    """Validate a due date string.

    Args:
        due_date: Candidate date string.

    Returns:
        The validated date string.

    Raises:
        ValueError: If it does not match YYYY-MM-DD or is not a real date.
    """
    if not isinstance(due_date, str) or not _DATE_RE.match(due_date):
        raise ValueError(f"due_date must be YYYY-MM-DD, got {due_date!r}")
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"due_date is not a real date: {due_date!r}") from exc
    return due_date


def add_deadline(
    store: Store,
    client_id: str,
    title: str,
    due_date: str,
    related_type: str | None = None,
    related_id: str | None = None,
    notes: str = "",
) -> dict:
    """Add a deadline for a client.

    Args:
        store: The Store.
        client_id: The client identifier.
        title: Deadline title.
        due_date: Due date as ``YYYY-MM-DD``.
        related_type: Optional related record type (e.g. ``"project"``).
        related_id: Optional related record ID.
        notes: Free-form notes.

    Returns:
        The created deadline dict (``done`` is False).

    Raises:
        ValueError: If ``due_date`` is not a valid YYYY-MM-DD date.
    """
    _validate_due_date(due_date)
    deadline: dict[str, Any] = {
        "id": store.new_id("ddl_"),
        "client_id": client_id,
        "title": title,
        "due_date": due_date,
        "related_type": related_type,
        "related_id": related_id,
        "notes": notes,
        "done": False,
        "created_at": utc_now_iso(),
    }
    store.append_json_list(client_id, deadline, _DEADLINES_FILE)
    return deadline


def list_deadlines(
    store: Store, client_id: str, include_done: bool = False
) -> list[dict]:
    """List a client's deadlines sorted by due date (ascending).

    Args:
        store: The Store.
        client_id: The client identifier.
        include_done: When False (default), hide completed deadlines.

    Returns:
        Deadlines sorted by ``due_date``.
    """
    deadlines = store.read_json(client_id, _DEADLINES_FILE, default=[])
    if not isinstance(deadlines, list):
        return []
    if not include_done:
        deadlines = [d for d in deadlines if not d.get("done")]
    return sorted(deadlines, key=lambda d: str(d.get("due_date", "")))


def mark_done(store: Store, client_id: str, deadline_id: str) -> dict:
    """Mark a deadline as done.

    Args:
        store: The Store.
        client_id: The client identifier.
        deadline_id: The deadline identifier.

    Returns:
        The updated deadline dict.

    Raises:
        ValueError: If the deadline does not exist.
    """
    deadlines = store.read_json(client_id, _DEADLINES_FILE, default=[])
    if not isinstance(deadlines, list):
        raise ValueError(f"deadline not found: {deadline_id!r}")
    for deadline in deadlines:
        if deadline.get("id") == deadline_id:
            deadline["done"] = True
            deadline["done_at"] = utc_now_iso()
            store.write_json(client_id, deadlines, _DEADLINES_FILE)
            return deadline
    raise ValueError(f"deadline not found: {deadline_id!r}")

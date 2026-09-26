"""Delivery providers: address validation, send abstraction, delivery logging."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any


class DeliveryError(Exception):
    """Raised when an address fails validation or a send fails."""


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _utcnow() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class DeliveryProvider(ABC):
    """Base class for delivery providers.

    Subclasses implement :meth:`send`. The default ``dry_run=True`` means
    nothing is actually transmitted.
    """

    name = "base"

    def validate_address(self, to: str) -> str:
        """Validate an email address.

        Returns the stripped address. Raises :class:`DeliveryError` when
        the address does not match :data:`EMAIL_RE`.
        """
        if not isinstance(to, str) or not EMAIL_RE.match(to.strip()):
            raise DeliveryError(f"invalid email address: {to!r}")
        return to.strip()

    @abstractmethod
    def send(self, to: str, subject: str, body: str, *, dry_run: bool = True) -> dict:
        """Send a message. ``dry_run=True`` (default) must not transmit."""
        raise NotImplementedError


def log_delivery(store: Any, client_id: str, record: dict) -> dict:
    """Append a delivery record to the client's ``delivery_log.json``.

    Only the ``{provider, to, subject, status, at}`` fields are persisted.
    Returns the stored entry.
    """
    entry = {
        "provider": record.get("provider"),
        "to": record.get("to"),
        "subject": record.get("subject"),
        "status": record.get("status"),
        "at": record.get("at") or _utcnow(),
    }
    store.append_json_list(client_id, entry, "delivery_log.json")
    return entry


def get_delivery_log(store: Any, client_id: str) -> list[dict]:
    """Return the client's delivery log (empty list when none exists)."""
    return store.read_json(client_id, "delivery_log.json", default=[]) or []

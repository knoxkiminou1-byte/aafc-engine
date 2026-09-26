"""Client records: create, dedup by email, promote, update."""

from __future__ import annotations

import re
from typing import Any

from .store import Store, utc_now_iso

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_CLIENT_FILE = "client.json"


class DuplicateClientError(Exception):
    """Raised when a distinct client conflicts on a unique field."""


class ClientNotFoundError(Exception):
    """Raised when a client ID does not exist in the store."""


def _validate_email(email: str) -> str:
    """Strip and lowercase an email address, validating its shape.

    Args:
        email: Raw email input.

    Returns:
        Normalized email.

    Raises:
        ValueError: If the email is empty or does not match EMAIL_RE.
    """
    normalized = (email or "").strip().lower()
    if not EMAIL_RE.match(normalized):
        raise ValueError(f"invalid email address: {email!r}")
    return normalized


def create_client(
    store: Store,
    name: str,
    email: str,
    business_name: str | None = None,
    phone: str | None = None,
    notes: str = "",
    source: str | None = None,
) -> tuple[dict, bool]:
    """Create a client record, deduping by email (case-insensitive).

    The dedup scan only lists client directories and reads their
    ``client.json`` files, per the isolation rule.

    Args:
        store: The Store.
        name: Client's name.
        email: Client's email; validated, stripped, lowercased.
        business_name: Optional business name.
        phone: Optional phone number.
        notes: Free-form notes.
        source: Optional acquisition source.

    Returns:
        Tuple of (client_dict, created). ``created`` is False when a client
        with the same email already existed, in which case the existing
        record is returned unchanged.

    Raises:
        ValueError: On an invalid email address.
    """
    email = _validate_email(email)
    existing = find_client_by_email(store, email)
    if existing is not None:
        return existing, False

    now = utc_now_iso()
    client: dict[str, Any] = {
        "id": store.new_id("cl_"),
        "name": name,
        "email": email,
        "business_name": business_name,
        "phone": phone,
        "notes": notes,
        "source": source,
        "record_type": "LEAD",
        "created_at": now,
        "updated_at": now,
    }
    store.write_json(client["id"], client, _CLIENT_FILE)
    return client, True


def get_client(store: Store, client_id: str) -> dict:
    """Fetch a client record by ID.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        The client dict.

    Raises:
        ClientNotFoundError: If the client does not exist.
    """
    client = store.read_json(client_id, _CLIENT_FILE)
    if client is None:
        raise ClientNotFoundError(f"client not found: {client_id!r}")
    return client


def list_clients(store: Store) -> list[dict]:
    """Return every client record in the store.

    Args:
        store: The Store.

    Returns:
        List of client dicts.
    """
    return [get_client(store, cid) for cid in store.list_client_ids()]


def find_client_by_email(store: Store, email: str) -> dict | None:
    """Find a client by email address (case-insensitive).

    Args:
        store: The Store.
        email: Email to search for.

    Returns:
        The matching client dict, or None.
    """
    wanted = (email or "").strip().lower()
    for client_id in store.list_client_ids():
        client = store.read_json(client_id, _CLIENT_FILE)
        if client and str(client.get("email", "")).lower() == wanted:
            return client
    return None


def promote_to_client(store: Store, client_id: str) -> dict:
    """Promote a LEAD record to CLIENT. Idempotent.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        The updated client dict.

    Raises:
        ClientNotFoundError: If the client does not exist.
    """
    client = get_client(store, client_id)
    if client.get("record_type") != "CLIENT":
        client["record_type"] = "CLIENT"
        client["updated_at"] = utc_now_iso()
        store.write_json(client_id, client, _CLIENT_FILE)
    return client


def update_client(store: Store, client_id: str, **fields: Any) -> dict:
    """Update mutable fields on a client record.

    The ``id`` and ``created_at`` fields are protected and cannot be changed.

    Args:
        store: The Store.
        client_id: The client identifier.
        **fields: Fields to update.

    Returns:
        The updated client dict.

    Raises:
        ClientNotFoundError: If the client does not exist.
    """
    client = get_client(store, client_id)
    for protected in ("id", "created_at"):
        fields.pop(protected, None)
    client.update(fields)
    client["updated_at"] = utc_now_iso()
    store.write_json(client_id, client, _CLIENT_FILE)
    return client

"""Customer filing cabinets for the AAFC business engine.

The customer file is an assembled, read-only view of everything the engine
knows about one client: identity, contact, source attribution, websites,
social profiles, offerings, communications, audits, findings, opportunities,
proposals, projects, deliverables, money, and history.

Email is the key: customers are resolved and deduped by email address
(case-insensitive), reusing the base ``clients`` module's dedup rule.

This module NEVER touches revenue records: it reads ``money.json`` directly
(read-only) only to include existing money events in the cabinet view. It
never imports or calls ``money.py``. Recommendation != revenue.
"""

from __future__ import annotations

from typing import Any

from . import clients, projects, reports, websites
from .auditor import registry as audit_registry
from .store import Store, utc_now_iso

_HISTORY_FILE = "history.json"
_OFFERINGS_FILE = "offerings.json"
_COMMUNICATIONS_FILE = "communications.json"


def _normalize_email(email: str) -> str:
    """Strip and lowercase an email address.

    Args:
        email: Raw email input.

    Returns:
        Normalized email (may be empty if input was empty).
    """
    return (email or "").strip().lower()


def log_history(store: Store, client_id: str, event: str, details: str = "") -> dict:
    """Append an event to a client's append-only history log.

    Args:
        store: The Store.
        client_id: The client identifier.
        event: Event name (e.g. ``"customer_registered"``, ``"source_set"``,
            ``"footprint_discovered"``, ``"social_audited"``,
            ``"crosschecked"``, ``"opportunity_map_built"``,
            ``"opportunity_status_changed"``, ``"intake"``).
        details: Optional free-form detail string.

    Returns:
        The appended history entry ``{"at", "event", "details"}``.
    """
    entry = {"at": utc_now_iso(), "event": event, "details": details}
    store.append_json_list(client_id, entry, _HISTORY_FILE)
    return entry


def get_customer_file(store: Store, email_or_client_id: str) -> dict:
    """Assemble a customer's full filing cabinet (read-only view).

    Pulls together every record the engine holds for the client without
    restructuring any base files. Missing data stays as empty lists /
    ``None`` / ``"UNKNOWN"`` -- never fabricated.

    Args:
        store: The Store.
        email_or_client_id: Client email or client id.

    Returns:
        Dict with keys: identity{name,email}, contact{phone},
        source{source,detail}, business{business_name}, websites[],
        social_profiles[], offerings[], communications[], audits[],
        findings[] (all findings across audits), opportunities[],
        proposals[] (projects with stage PROPOSAL), projects[],
        deliverables[] (reports), money[], history[].

    Raises:
        clients.ClientNotFoundError: If no client matches.
    """
    client = _load_client(store, email_or_client_id)
    client_id = client["id"]

    from . import sources  # local import to avoid a module-level cycle

    source = sources.get_source(store, client_id)
    website_list = websites.list_websites(store, client_id)
    social_doc = store.read_json(client_id, "social.json", default={}) or {}
    social_profiles = social_doc.get("profiles", [])
    audits = audit_registry.list_audits(store, client_id)
    findings_all = [
        finding for audit in audits for finding in audit.get("findings", [])
    ]
    opportunities_doc = store.read_json(client_id, "opportunities.json", default={}) or {}
    project_list = projects.list_projects(store, client_id)
    proposals = [p for p in project_list if p.get("stage") == "PROPOSAL"]
    deliverables = reports.list_reports(store, client_id)
    # Read-only: money records are shown in the cabinet but this module
    # never imports or calls money.py (recommendation != revenue).
    money_events = store.read_json(client_id, "money.json", default=[]) or []
    history = store.read_json(client_id, _HISTORY_FILE, default=[]) or []

    return {
        "identity": {"name": client.get("name"), "email": client.get("email")},
        "contact": {"phone": client.get("phone")},
        "source": {"source": source.get("source"), "detail": source.get("detail")},
        "business": {"business_name": client.get("business_name")},
        "websites": website_list,
        "social_profiles": social_profiles,
        "offerings": store.read_json(client_id, _OFFERINGS_FILE, default=[]) or [],
        "communications": store.read_json(client_id, _COMMUNICATIONS_FILE, default=[]) or [],
        "audits": audits,
        "findings": findings_all,
        "opportunities": opportunities_doc.get("opportunities", []),
        "proposals": proposals,
        "projects": project_list,
        "deliverables": deliverables,
        "money": money_events,
        "history": history,
    }


def _load_client(store: Store, email_or_client_id: str) -> dict:
    """Load a client by id, falling back to an email lookup.

    Args:
        store: The Store.
        email_or_client_id: Client id or email address.

    Returns:
        The client dict.

    Raises:
        clients.ClientNotFoundError: If no client matches.
    """
    text = (email_or_client_id or "").strip()
    if store.client_exists(text):
        return clients.get_client(store, text)
    found = clients.find_client_by_email(store, text)
    if found is not None:
        return found
    raise clients.ClientNotFoundError(f"customer not found: {email_or_client_id!r}")


def resolve_customer(store: Store, email: str) -> dict:
    """Resolve a customer by email: FOUND (with full cabinet) or NEW.

    Uses ``clients.find_client_by_email`` (case-insensitive). Missing data
    is never fabricated: a NEW resolution returns ``customer=None``.

    Args:
        store: The Store.
        email: Email address to resolve.

    Returns:
        ``{"resolution": "FOUND"|"NEW", "email": normalized, "customer":
        cabinet|None}``.
    """
    normalized = _normalize_email(email)
    found = clients.find_client_by_email(store, normalized)
    if found is None:
        return {"resolution": "NEW", "email": normalized, "customer": None}
    return {
        "resolution": "FOUND",
        "email": normalized,
        "customer": get_customer_file(store, found["id"]),
    }


def register_customer(
    store: Store,
    name: str,
    email: str,
    business_name: str | None = None,
    phone: str | None = None,
    source: str = "UNKNOWN",
    source_detail: str = "",
    notes: str = "",
) -> dict:
    """Register a customer with minimal data collection only.

    Creates the base client record (record_type LEAD), records source
    attribution via :mod:`sources` (with permanence + history logging),
    initializes the empty extension-layer cabinet files, and logs
    ``"customer_registered"``. If a client with the same email already
    exists, the existing record is returned (with its cabinet) and no
    duplicate is created.

    Args:
        store: The Store.
        name: Customer's name.
        email: Customer's email (validated, stripped, lowercased).
        business_name: Optional business name.
        phone: Optional phone number.
        source: Acquisition source; must be in :data:`sources.SOURCES`.
        source_detail: Optional detail about the source.
        notes: Optional free-form notes.

    Returns:
        The customer's full filing cabinet dict.
    """
    from . import sources  # local import to avoid a module-level cycle

    client, created = clients.create_client(
        store,
        name,
        email,
        business_name=business_name,
        phone=phone,
        notes=notes,
    )
    client_id = client["id"]
    if created:
        # Initialize empty extension-layer cabinet files.
        for filename in (_OFFERINGS_FILE, _COMMUNICATIONS_FILE):
            if store.read_json(client_id, filename, default=None) is None:
                store.write_json(client_id, [], filename)
        sources.set_source(store, client_id, source, detail=source_detail)
        log_history(
            store,
            client_id,
            "customer_registered",
            f"registered via self-service intake; email={client['email']}",
        )
    return get_customer_file(store, client_id)


def self_service_intake(store: Store, name: str, email: str) -> dict:
    """Run the self-service intake flow for a customer.

    FOUND: loads the existing cabinet and the primary website (the first
    website on record); asks for nothing again, ``needs=[]``.
    NEW: registers the customer minimally; ``needs=["website",
    "authorization"]`` (their website URL, plus authorization to audit it).

    Args:
        store: The Store.
        name: Customer's name (used only when registering).
        email: Customer's email.

    Returns:
        ``{"resolution", "customer", "primary_website"|None,
        "needs": [...]}``.
    """
    resolution = resolve_customer(store, email)
    if resolution["resolution"] == "NEW":
        cabinet = register_customer(store, name, email)
        log_history(
            store,
            _client_id(store, email),
            "intake",
            "self-service intake: new customer registered",
        )
        return {
            "resolution": "NEW",
            "customer": cabinet,
            "primary_website": None,
            "needs": ["website", "authorization"],
        }
    cabinet = resolution["customer"]
    website_list = cabinet.get("websites", [])
    primary = website_list[0] if website_list else None
    log_history(
        store,
        _client_id(store, email),
        "intake",
        "self-service intake: returning customer recognized",
    )
    return {
        "resolution": "FOUND",
        "customer": cabinet,
        "primary_website": primary,
        "needs": [],
    }


def _client_id(store: Store, email: str) -> str:
    """Return the client id for an email known to exist.

    Args:
        store: The Store.
        email: Email address known to resolve to a client.

    Returns:
        The client id.

    Raises:
        clients.ClientNotFoundError: If no client matches.
    """
    found = clients.find_client_by_email(store, email)
    if found is None:
        raise clients.ClientNotFoundError(f"customer not found: {email!r}")
    return found["id"]


def add_offering(
    store: Store,
    client_id: str,
    name: str,
    status: str = "current",
    notes: str = "",
) -> dict:
    """Record a customer's product or service offering.

    Args:
        store: The Store.
        client_id: The client identifier.
        name: Offering name.
        status: ``"current"`` or ``"past"``.
        notes: Optional notes.

    Returns:
        The appended offering dict.

    Raises:
        ValueError: If status is not ``"current"`` or ``"past"``.
    """
    if status not in ("current", "past"):
        raise ValueError(f"offering status must be 'current' or 'past': {status!r}")
    clients.get_client(store, client_id)  # raises if unknown
    offering = {
        "id": store.new_id("off_"),
        "name": name,
        "status": status,
        "notes": notes,
        "added_at": utc_now_iso(),
    }
    store.append_json_list(client_id, offering, _OFFERINGS_FILE)
    return offering


def log_communication(
    store: Store,
    client_id: str,
    channel: str,
    direction: str,
    subject: str,
    body_ref: str = "",
) -> dict:
    """Log a customer communication (metadata only, never full secrets).

    Only a ``body_ref`` (e.g. a message id or file path) is stored -- never
    the full message body, and never secrets.

    Args:
        store: The Store.
        client_id: The client identifier.
        channel: Communication channel (e.g. ``"email"``, ``"phone"``).
        direction: ``"inbound"`` or ``"outbound"``.
        subject: Message subject or summary.
        body_ref: Reference to the full message (message id, file path).

    Returns:
        The appended communication dict.
    """
    clients.get_client(store, client_id)  # raises if unknown
    entry = {
        "id": store.new_id("com_"),
        "channel": channel,
        "direction": direction,
        "subject": subject,
        "body_ref": body_ref,
        "logged_at": utc_now_iso(),
    }
    store.append_json_list(client_id, entry, _COMMUNICATIONS_FILE)
    return entry

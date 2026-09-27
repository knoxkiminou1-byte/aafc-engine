"""Money events: invoices, payments, and the receivable evidence HARD RULE.

Amounts are stored as integer cents (``round(dollars * 100)``). A receivable
status (OWED / EXPECTED / OVERDUE) with no evidence is refused outright:
the engine never manufactures money it cannot prove.
"""

from __future__ import annotations

from typing import Any

from .store import Store, utc_now_iso

MONEY_STATES = [
    "PAID",
    "OWED",
    "EXPECTED",
    "PARTIALLY_PAID",
    "OVERDUE",
    "NEEDS_VERIFICATION",
    "CANCELLED",
]
MONEY_KINDS = ["PROPOSAL", "INVOICE", "DEPOSIT", "PAYMENT", "BALANCE"]

#: Statuses that assert someone owes money and therefore require evidence.
RECEIVABLE_STATES = {"OWED", "EXPECTED", "OVERDUE"}

#: Terminal statuses: a money event in one of these can never move again.
TERMINAL_STATES = {"PAID", "CANCELLED"}

#: Allowed status transitions. ``PAID`` and ``CANCELLED`` are terminal and
#: have no outgoing edges; every other status maps to the statuses it may
#: legally move to. Anything not listed here is refused.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "NEEDS_VERIFICATION": frozenset({"EXPECTED", "OWED", "CANCELLED"}),
    "EXPECTED": frozenset({"OWED", "OVERDUE", "PARTIALLY_PAID", "PAID", "CANCELLED"}),
    "OWED": frozenset({"OVERDUE", "PARTIALLY_PAID", "PAID", "CANCELLED"}),
    "OVERDUE": frozenset({"PARTIALLY_PAID", "PAID", "CANCELLED"}),
    "PARTIALLY_PAID": frozenset({"PAID", "CANCELLED"}),
    "PAID": frozenset(),
    "CANCELLED": frozenset(),
}

_MONEY_FILE = "money.json"


class MoneyError(ValueError):
    """Raised for invalid money events or status transitions."""


def _to_cents(dollars: float) -> int:
    """Convert a dollar amount to integer cents.

    Args:
        dollars: Dollar amount.

    Returns:
        Integer cents via ``round(dollars * 100)``.
    """
    return int(round(float(dollars) * 100))


def _find_event(store: Store, client_id: str, event_id: str) -> tuple[dict, list[dict]]:
    """Locate an event and its backing list.

    Returns:
        Tuple of (event_dict, events_list).

    Raises:
        MoneyError: If the event does not exist.
    """
    events = store.read_json(client_id, _MONEY_FILE, default=[])
    if not isinstance(events, list):
        raise MoneyError(f"money ledger for client {client_id!r} is corrupt")
    for event in events:
        if event.get("id") == event_id:
            return event, events
    raise MoneyError(f"money event not found: {event_id!r}")


def record_event(
    store: Store,
    client_id: str,
    kind: str,
    amount_dollars: float,
    status: str,
    evidence: str = "",
    project_id: str | None = None,
    due_date: str | None = None,
    notes: str = "",
    amount_paid_dollars: float = 0.0,
) -> dict:
    """Record a money event.

    Args:
        store: The Store.
        client_id: The client identifier.
        kind: One of MONEY_KINDS.
        amount_dollars: Total amount in dollars (stored as integer cents).
        status: One of MONEY_STATES.
        evidence: Proof backing the event. REQUIRED (non-empty) when
            ``status`` is OWED, EXPECTED, or OVERDUE.
        project_id: Optional linked project.
        due_date: Optional due date string.
        notes: Free-form notes.
        amount_paid_dollars: For PARTIALLY_PAID, the amount already paid;
            must be strictly between 0 and ``amount_dollars``.

    Returns:
        The created event dict.

    Raises:
        MoneyError: On invalid kind/status, on a receivable status without
            evidence, or on an invalid PARTIALLY_PAID paid amount.
    """
    if kind not in MONEY_KINDS:
        raise MoneyError(f"invalid money kind {kind!r}; expected one of {MONEY_KINDS}")
    if status not in MONEY_STATES:
        raise MoneyError(
            f"invalid money status {status!r}; expected one of {MONEY_STATES}"
        )
    if status in RECEIVABLE_STATES and not (evidence or "").strip():
        raise MoneyError(
            "refusing to manufacture a receivable: "
            f"status {status!r} requires non-empty evidence"
        )

    amount_cents = _to_cents(amount_dollars)
    amount_paid_cents = _to_cents(amount_paid_dollars)
    if amount_cents < 0 or amount_paid_cents < 0:
        raise MoneyError(
            "refusing to record a negative money amount "
            f"(amount={amount_cents}c, paid={amount_paid_cents}c)"
        )
    if status == "PARTIALLY_PAID":
        if not 0 < amount_paid_cents < amount_cents:
            raise MoneyError(
                "PARTIALLY_PAID requires 0 < amount_paid < amount "
                f"(got paid={amount_paid_cents}c of {amount_cents}c)"
            )

    event: dict[str, Any] = {
        "id": store.new_id("mon_"),
        "client_id": client_id,
        "project_id": project_id,
        "kind": kind,
        "amount_cents": amount_cents,
        "amount_paid_cents": amount_paid_cents,
        "currency": "USD",
        "status": status,
        "evidence": evidence,
        "due_date": due_date,
        "notes": notes,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "status_history": [],
    }
    store.append_json_list(client_id, event, _MONEY_FILE)
    return event


def set_event_status(
    store: Store,
    client_id: str,
    event_id: str,
    new_status: str,
    evidence: str | None = None,
    amount_paid_dollars: float | None = None,
) -> dict:
    """Transition a money event to a new status.

    Args:
        store: The Store.
        client_id: The client identifier.
        event_id: The event identifier.
        new_status: Target status (must be in MONEY_STATES).
        evidence: Optional new evidence. Required when moving INTO a
            receivable status if the event has none, and always required
            when moving to PAID (it must be evidence *of payment*).
        amount_paid_dollars: Required when moving to PARTIALLY_PAID; the
            amount already paid must satisfy 0 < paid < amount.

    Returns:
        The updated event dict.

    Raises:
        MoneyError: On an invalid status, missing event, missing
            required evidence, or an invalid partial-payment amount.
    """
    if new_status not in MONEY_STATES:
        raise MoneyError(
            f"invalid money status {new_status!r}; expected one of {MONEY_STATES}"
        )
    event, events = _find_event(store, client_id, event_id)
    old_status = event.get("status")

    if new_status == old_status:
        # Idempotent: re-applying the current status changes nothing.
        return event
    if new_status not in ALLOWED_TRANSITIONS.get(old_status, frozenset()):
        raise MoneyError(
            f"illegal money transition {old_status!r} -> {new_status!r}; "
            f"allowed from {old_status!r}: "
            f"{sorted(ALLOWED_TRANSITIONS.get(old_status, ())) or 'none (terminal)'}"
        )

    if new_status in RECEIVABLE_STATES:
        effective = (evidence if evidence else event.get("evidence") or "")
        if not effective.strip():
            raise MoneyError(
                "refusing to manufacture a receivable: "
                f"moving to {new_status!r} requires non-empty evidence"
            )
        if evidence:
            event["evidence"] = evidence

    if new_status == "PARTIALLY_PAID":
        if amount_paid_dollars is None:
            raise MoneyError(
                "moving to PARTIALLY_PAID requires amount_paid_dollars "
                "(the amount already paid)"
            )
        paid_cents = _to_cents(amount_paid_dollars)
        amount_cents = event.get("amount_cents", 0)
        if not 0 < paid_cents < amount_cents:
            raise MoneyError(
                "PARTIALLY_PAID requires 0 < amount_paid < amount "
                f"(got paid={paid_cents}c of {amount_cents}c)"
            )
        event["amount_paid_cents"] = paid_cents

    if new_status == "PAID":
        if not (evidence or "").strip():
            raise MoneyError(
                "moving to PAID requires evidence of payment "
                "(pass evidence= describing the payment)"
            )
        event["evidence"] = evidence

    event["status"] = new_status
    event["updated_at"] = utc_now_iso()
    history = event.setdefault("status_history", [])
    if isinstance(history, list):
        history.append(
            {
                "from": old_status,
                "to": new_status,
                "at": event["updated_at"],
            }
        )
    store.write_json(client_id, events, _MONEY_FILE)
    return event


def list_events(store: Store, client_id: str) -> list[dict]:
    """List all money events for a client.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        List of event dicts.
    """
    events = store.read_json(client_id, _MONEY_FILE, default=[])
    return events if isinstance(events, list) else []


def outstanding_cents(store: Store, client_id: str) -> int:
    """Compute the outstanding balance in cents.

    Counts the full amount of OWED and OVERDUE events, plus the unpaid
    remainder of PARTIALLY_PAID events. All other statuses are ignored.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        Outstanding amount in integer cents.
    """
    total = 0
    for event in list_events(store, client_id):
        status = event.get("status")
        if status in ("OWED", "OVERDUE"):
            total += int(event.get("amount_cents", 0))
        elif status == "PARTIALLY_PAID":
            total += int(event.get("amount_cents", 0)) - int(
                event.get("amount_paid_cents", 0)
            )
    return total

"""SwiftSend delivery — explicitly BLOCKED.

No SwiftSend API exists in this workspace, so this provider can never
send. The stub is required to be explicit about what is missing.
"""
from __future__ import annotations

from .base import DeliveryProvider


class SwiftSendDelivery(DeliveryProvider):
    """Placeholder provider: SwiftSend has no API in this workspace."""

    name = "swiftsend"

    def send(self, to: str, subject: str, body: str, *, dry_run: bool = True) -> dict:
        """Always raises: there is no SwiftSend API to call."""
        raise NotImplementedError(
            "SwiftSend delivery is BLOCKED: no SwiftSend API exists in this workspace. "
            "Needed before implementation: API base URL, auth scheme, and endpoint "
            "contract from the account owner."
        )

"""Gmail delivery via the hatch_gws_cli helper.

Real transmission happens only when ``dry_run=False``; the default
``dry_run=True`` returns a DRY_RUN record without sending anything.
"""
from __future__ import annotations

import subprocess

from .base import DeliveryError, DeliveryProvider, _utcnow


class GmailDelivery(DeliveryProvider):
    """Send email through Gmail using the ``hatch_gws_cli`` command."""

    name = "gmail"

    def send(self, to: str, subject: str, body: str, *, dry_run: bool = True) -> dict:
        """Send a message via Gmail.

        Validates the address first. With ``dry_run=True`` returns a
        DRY_RUN record and sends nothing. With ``dry_run=False`` runs
        ``hatch_gws_cli gmail +send`` (60s timeout); returncode 0 yields a
        SENT record, otherwise :class:`DeliveryError` with the stderr tail.
        """
        to = self.validate_address(to)
        if dry_run:
            return {
                "provider": self.name,
                "to": to,
                "subject": subject,
                "status": "DRY_RUN",
                "at": _utcnow(),
            }
        try:
            proc = subprocess.run(
                [
                    "hatch_gws_cli", "gmail", "+send",
                    "--to", to,
                    "--subject", subject,
                    "--body", body,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DeliveryError(f"gmail send failed: {exc}") from exc
        if proc.returncode != 0:
            stderr_tail = "\n".join((proc.stderr or "").strip().splitlines()[-5:])
            raise DeliveryError(f"gmail send failed (exit {proc.returncode}): {stderr_tail}")
        return {
            "provider": self.name,
            "to": to,
            "subject": subject,
            "status": "SENT",
            "at": _utcnow(),
        }

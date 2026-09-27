"""SSRF-hardened HTTP GET for the business-layer fetchers.

The vendored audit engine enforces its own ``FETCH_GUARD`` (private-IP
rejection + redirect-target revalidation) in ``aafc_engine/auditor/engine.py``
and ``api/index.py``. The business-layer modules that fetch on their own --
``footprint`` (customer homepage) and ``social_audit`` (profile URLs pulled
from that homepage's HTML) -- must not be a softer path, so they share this
helper instead of calling ``requests.get`` directly.

Defense in depth (matches the engine's posture, adapted for these modules):

- Only ``http``/``https`` schemes are fetched.
- The host is DNS-resolved up front and *every* resolved address must be a
  globally routable IP (private, loopback, link-local, multicast,
  unspecified, and reserved ranges are refused). ``localhost`` is therefore
  refused here even though ``websites.validate_url`` permits it for tests --
  these fetchers only ever target real customer websites.
- Redirects are followed manually (never ``allow_redirects=True``) up to
  ``max_redirects`` hops, and every redirect target is re-resolved and
  re-validated before it is fetched.
- The response body is capped at ``max_bytes`` (streamed); oversized bodies
  are truncated and flagged rather than loaded into memory unbounded.

Residual limitation (documented, not hidden): the DNS check happens before
connect, so a hostile DNS that flips answers between check and connect
(rebinding) is not defeated here. These fetchers run on the operator's own
machine against customer websites the operator chose to audit, so
resolve-then-check is proportionate; the hosted engine path keeps its own
stricter guard.

Returns a dict with ``ok``, ``status``, ``text``, ``final_url``,
``truncated``, ``error``. Never raises on network/DNS/validation problems:
they come back as ``ok=False`` with an ``error`` string, because callers
treat fetch failure as honest "could not verify" evidence.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

#: Refuse anything that is not a globally routable address.
_MAX_REDIRECTS = 5
_MAX_BYTES = 5_000_000  # 5 MB: generous for HTML, fatal for nothing

_UA = {"User-Agent": "AAFC-footprint/1.0 (+https://aafc-engine.vercel.app)"}

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def host_is_public(host: str) -> tuple[bool, str]:
    """Check that ``host`` resolves only to globally routable IPs.

    Returns:
        ``(True, "")`` when every resolved address is global, else
        ``(False, reason)``.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for {host!r}: {exc}"
    if not infos:
        return False, f"no addresses resolved for {host!r}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"unparseable resolved address {ip_str!r} for {host!r}"
        if not ip.is_global:
            return False, (
                f"refusing non-public address {ip_str} for host {host!r} "
                "(private/loopback/link-local/reserved)"
            )
    return True, ""


def safe_get(
    url: str,
    timeout: float = 20.0,
    max_bytes: int = _MAX_BYTES,
    max_redirects: int = _MAX_REDIRECTS,
    user_agent: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET ``url`` with SSRF defense-in-depth (see module docstring)."""
    headers = user_agent or _UA
    current = url
    for _hop in range(max_redirects + 1):
        try:
            parsed = urlparse(current)
        except Exception as exc:
            return _fail(url, f"unparseable URL {current!r}: {exc}")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return _fail(url, f"refusing non-HTTP(S) or hostless URL {current!r}")
        ok, reason = host_is_public(parsed.hostname)
        if not ok:
            return _fail(url, reason)
        try:
            response = requests.get(
                current,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            return _fail(url, f"{type(exc).__name__}: {exc}")
        if response.status_code in _REDIRECT_STATUSES:
            location = response.headers.get("Location")
            response.close()
            if not location:
                return _fail(url, f"redirect without Location from {current!r}")
            current = urljoin(current, location)
            continue
        truncated = False
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    # Keep only up to the cap, then stop reading.
                    over = total - max_bytes
                    chunks.append(chunk[: len(chunk) - over])
                    truncated = True
                    break
                chunks.append(chunk)
        except requests.RequestException as exc:
            response.close()
            return _fail(url, f"{type(exc).__name__} while reading body: {exc}")
        finally:
            response.close()
        body = b"".join(chunks)
        try:
            text = body.decode(response.encoding or "utf-8", errors="replace")
        except Exception:
            text = body.decode("utf-8", errors="replace")
        return {
            "ok": True,
            "status": response.status_code,
            "text": text,
            "final_url": current,
            "truncated": truncated,
            "error": None,
        }
    return _fail(url, f"too many redirects (>{max_redirects}) starting at {url!r}")


def _fail(final_url: str, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": None,
        "text": "",
        "final_url": final_url,
        "truncated": False,
        "error": error,
    }

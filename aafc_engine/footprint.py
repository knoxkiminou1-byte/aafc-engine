"""Digital footprint discovery (public web only, read-only).

Given one of a customer's websites, fetches its homepage once and looks
for links to the customer's public social / business profiles, plus
basic contact signals (mailto:, tel:). Every claim is evidence-backed:
a homepage link to a platform profile is PUBLICLY VERIFIED with that
link as evidence; a platform with no link is NOT FOUND (never assumed).

google_business and directories are reported NOT FOUND with an honest
note, because no search API is configured -- we never fake a lookup.

Honest limitation (documented): this module makes read-only GET requests
to the customer's own public website, as authorized by the extension
spec. This extends the base contract's network rule (auditor engine and
Gmail real-send only) to read-only public footprint discovery.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from . import customers
from . import safefetch
from .store import Store, utc_now_iso
from .websites import get_website, require_authorized

UA = {"User-Agent": "AAFC-Footprint-Discovery/1.0 (read-only; public web)"}
TIMEOUT = 12

#: Platform -> list of domain patterns used to recognize profile links.
#: Platforms with an empty pattern list have no link extraction configured.
PLATFORMS: dict[str, list[str]] = {
    "instagram": ["instagram.com"],
    "facebook": ["facebook.com", "fb.com", "fb.me"],
    "linkedin": ["linkedin.com"],
    "youtube": ["youtube.com", "youtu.be"],
    "tiktok": ["tiktok.com"],
    "x": ["x.com", "twitter.com"],
    "google_business": [],
    "directories": [],
}


class _LinkParser(HTMLParser):
    """Collect <a href> links plus rel="me" identity links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href") or ""
        rel = (attrs_dict.get("rel") or "").lower()
        if href:
            self.links.append({"href": href.strip(), "rel": rel})


def _fetch_homepage(url: str) -> dict:
    """Fetch a homepage with the SSRF-hardened shared fetcher.

    Args:
        url: Homepage URL.

    Returns:
        Dict with ``ok``, ``status``, ``text``, ``final_url``, ``error``.
    """
    result = safefetch.safe_get(url, timeout=TIMEOUT, user_agent=UA)
    return {
        "ok": result["ok"],
        "status": result["status"],
        "text": result["text"],
        "final_url": result["final_url"],
        "error": result["error"],
    }


def _matches_platform(netloc: str, patterns: list[str]) -> bool:
    """Return True if a link host matches a platform's domain patterns."""
    host = netloc.lower()
    return any(host == p or host.endswith("." + p) for p in patterns)


def _extract_links(text: str) -> list[dict[str, str]]:
    """Extract all <a href> links from HTML (never raises on bad HTML)."""
    parser = _LinkParser()
    try:
        parser.feed(text)
    except Exception:
        pass  # partial extraction is fine; honesty lives in the evidence
    return parser.links


def discover(store: Store, client_id: str, website_id: str) -> list[dict]:
    """Discover a customer's public digital footprint from their homepage.

    Fetches the website homepage once, extracts ``<a href>`` links matching
    known platform domains (plus ``rel="me"`` identity links), and counts
    contact signals (``mailto:`` / ``tel:`` links). Saves the full record to
    ``social.json`` and logs ``"footprint_discovered"``.

    Args:
        store: The Store.
        client_id: The client identifier.
        website_id: The website identifier.

    Returns:
        List of ``{"platform", "url"|None, "status",
        "confidence", "evidence"}`` dicts, one per platform in
        :data:`PLATFORMS`.
    """
    website = get_website(store, client_id, website_id)
    require_authorized(website)
    homepage = website["url"]

    fetch_result = _fetch_homepage(homepage)
    links: list[dict[str, str]] = []
    contact_signals = {"mailto_count": 0, "tel_count": 0}
    if fetch_result["ok"]:
        links = _extract_links(fetch_result["text"])
        for link in links:
            href = link["href"].lower()
            if href.startswith("mailto:"):
                contact_signals["mailto_count"] += 1
            elif href.startswith("tel:"):
                contact_signals["tel_count"] += 1

    profiles: list[dict[str, Any]] = []
    for platform, patterns in PLATFORMS.items():
        if not patterns:
            profiles.append(
                {
                    "platform": platform,
                    "url": None,
                    "status": "NOT FOUND",
                    "confidence": "UNVERIFIED",
                    "evidence": (
                        "no search API configured; manual lookup required "
                        f"for {platform}"
                    ),
                }
            )
            continue
        if not fetch_result["ok"]:
            profiles.append(
                {
                    "platform": platform,
                    "url": None,
                    "status": "NOT FOUND",
                    "confidence": "UNVERIFIED",
                    "evidence": (
                        f"homepage fetch failed ({fetch_result['error']}); "
                        f"cannot verify {platform} presence"
                    ),
                }
            )
            continue
        match_url: str | None = None
        match_rel_me = False
        for link in links:
            parsed = urlparse(link["href"])
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            if _matches_platform(parsed.netloc, patterns):
                match_url = link["href"]
                match_rel_me = "me" in link["rel"].split()
                break
        if match_url:
            evidence = f"homepage link {match_url} on {homepage}"
            if match_rel_me:
                evidence += " (rel=\"me\" identity link)"
            profiles.append(
                {
                    "platform": platform,
                    "url": match_url,
                    "status": "PUBLICLY VERIFIED",
                    "confidence": "CONFIRMED",
                    "evidence": evidence,
                }
            )
        else:
            profiles.append(
                {
                    "platform": platform,
                    "url": None,
                    "status": "NOT FOUND",
                    "confidence": "UNVERIFIED",
                    "evidence": (
                        f"no link to {platform} found on homepage {homepage}"
                    ),
                }
            )

    record = {
        "website_id": website_id,
        "website_url": homepage,
        "discovered_at": utc_now_iso(),
        "homepage_fetch": {
            "ok": fetch_result["ok"],
            "status": fetch_result["status"],
            "error": fetch_result["error"],
        },
        "profiles": profiles,
        "contact_signals": contact_signals,
    }
    store.write_json(client_id, record, "social.json")

    verified = sum(1 for p in profiles if p["status"] == "PUBLICLY VERIFIED")
    customers.log_history(
        store,
        client_id,
        "footprint_discovered",
        f"{verified} of {len(profiles)} platforms publicly verified "
        f"from {homepage}",
    )
    return profiles

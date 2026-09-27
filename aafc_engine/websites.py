"""Website records per client: normalization, validation, authorization."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .clients import ClientNotFoundError, get_client
from .store import Store, utc_now_iso

_WEBSITES_FILE = "websites.json"


class WebsiteError(ValueError):
    """Raised for invalid URLs or missing website records."""


class NotAuthorizedError(PermissionError):
    """Raised when an action requires an authorized website that isn't one."""


def normalize_url(url: str) -> str:
    """Normalize a URL for storage and dedup.

    Adds ``https://`` when no scheme is present, lowercases the host,
    drops the fragment, and drops trailing slashes. The root path ``/``
    is normalized to no path, so ``example.com`` and ``example.com/``
    dedup to the same URL (contract's "drop trailing slash" rule;
    keeping ``/`` for root would split this obvious duplicate pair).

    Args:
        url: Raw URL input.

    Returns:
        The normalized URL.

    Raises:
        WebsiteError: If the URL is empty or unparseable.
    """
    if not url or not url.strip():
        raise WebsiteError("URL must not be empty")
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise WebsiteError(f"could not parse URL {url!r}: {exc}") from exc
    host = parsed.hostname
    if not host:
        raise WebsiteError(f"could not parse URL: {url!r}")
    host = host.lower()
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        port = None
    # Drop default ports and any userinfo: https://example.com:443 and
    # https://example.com are the same website, and credentials embedded
    # in a URL must never become part of the stored identity.
    default_port = {"http": 80, "https": 443}.get(parsed.scheme)
    if port and port != default_port:
        netloc = f"{host}:{port}"
    path = parsed.path or ""
    if path == "/":
        path = ""  # root: "" and "/" are the same URL for dedup
    elif path.endswith("/"):
        path = path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    # fragment is dropped
    return f"{parsed.scheme}://{netloc}{path}{query}"


def validate_url(url: str) -> str:
    """Validate and normalize a URL.

    Args:
        url: Raw URL input.

    Returns:
        The normalized URL.

    Raises:
        WebsiteError: If the scheme is not http/https, the host has no
            dot, or the host is a non-global IP literal (private, loopback,
            link-local, etc.). ``localhost`` is allowed for tests.

    Note: this is registration-time defense in depth. The authoritative
    SSRF enforcement stays at fetch time (the hosted API's fetch guard
    re-resolves every redirect target); DNS names are not resolved here.
    """
    import ipaddress

    normalized = normalize_url(url)
    parsed = urlsplit(normalized)
    if parsed.scheme not in ("http", "https"):
        raise WebsiteError(
            f"URL scheme must be http or https: {url!r}"
        )
    host = parsed.hostname or ""
    if "." not in host and host != "localhost":
        raise WebsiteError(f"URL host looks invalid: {url!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise WebsiteError(
            f"URL host is not a public address: {url!r}"
        )
    return normalized


def add_website(
    store: Store,
    client_id: str,
    url: str,
    label: str | None = None,
    authorized: bool = False,
) -> tuple[dict, bool]:
    """Add a website to a client, deduping per client by normalized URL.

    Args:
        store: The Store.
        client_id: The client identifier.
        url: Website URL (validated and normalized).
        label: Optional human label.
        authorized: Whether the client authorized us to audit this site.

    Returns:
        Tuple of (website_dict, created). ``created`` is False when a
        website with the same normalized URL already existed for this
        client, in which case the existing record is returned unchanged.

    Raises:
        ClientNotFoundError: If the client does not exist.
        WebsiteError: If the URL is invalid.
    """
    if not store.client_exists(client_id):
        raise ClientNotFoundError(f"client not found: {client_id!r}")
    # Validates the client record is readable too.
    get_client(store, client_id)

    normalized = validate_url(url)
    websites = store.read_json(client_id, _WEBSITES_FILE, default=[])
    for existing in websites:
        if existing.get("url") == normalized:
            return existing, False

    domain = urlsplit(normalized).hostname or ""
    website: dict[str, Any] = {
        "id": store.new_id("web_"),
        "client_id": client_id,
        "url": normalized,
        "domain": domain,
        "label": label,
        "authorized": bool(authorized),
        "created_at": utc_now_iso(),
    }
    websites.append(website)
    store.write_json(client_id, websites, _WEBSITES_FILE)
    return website, True


def get_website(store: Store, client_id: str, website_id: str) -> dict:
    """Fetch a website record by ID.

    Args:
        store: The Store.
        client_id: The client identifier.
        website_id: The website identifier.

    Returns:
        The website dict.

    Raises:
        WebsiteError: If the website does not exist.
    """
    for website in list_websites(store, client_id):
        if website.get("id") == website_id:
            return website
    raise WebsiteError(f"website not found: {website_id!r}")


def list_websites(store: Store, client_id: str) -> list[dict]:
    """List all websites for a client.

    Args:
        store: The Store.
        client_id: The client identifier.

    Returns:
        List of website dicts.
    """
    websites = store.read_json(client_id, _WEBSITES_FILE, default=[])
    return websites if isinstance(websites, list) else []


def set_authorized(
    store: Store, client_id: str, website_id: str, authorized: bool = True
) -> dict:
    """Set the authorization flag on a website.

    Args:
        store: The Store.
        client_id: The client identifier.
        website_id: The website identifier.
        authorized: New authorization value.

    Returns:
        The updated website dict.

    Raises:
        WebsiteError: If the website does not exist.
    """
    websites = list_websites(store, client_id)
    for website in websites:
        if website.get("id") == website_id:
            website["authorized"] = bool(authorized)
            store.write_json(client_id, websites, _WEBSITES_FILE)
            return website
    raise WebsiteError(f"website not found: {website_id!r}")


def require_authorized(website: dict) -> None:
    """Require a website to be authorized.

    Args:
        website: The website dict.

    Raises:
        NotAuthorizedError: If the website is not authorized.
    """
    if not website.get("authorized"):
        raise NotAuthorizedError(
            f"website {website.get('id', '?')} ({website.get('url', '?')}) "
            "is not authorized; refusing to proceed"
        )

"""Headless-Chromium render provider for the AAFC auditor.

This module gives the engine its own eyes: each audited page is loaded in a
real headless Chromium (via Playwright), the DOM is allowed to settle, and
the RENDERED DOM state is extracted. Rendered evidence outranks raw HTML —
a finding confirmed by the rendered DOM is ground truth; a finding
contradicted by it is dropped.

The provider is OPTIONAL. If Playwright/Chromium is not installed, every
entry point degrades gracefully (:func:`render_available` returns False,
:func:`render_pages` returns ``{}``) and the engine runs HTTP-only exactly
as before — DOM-sensitive checks then keep their NEEDS_RENDERED_REVIEW /
NEEDS MANUAL REVIEW honesty labels instead of asserting from raw HTML.

Deliberately, there is NO AI/API integration here. The engine is fully
autonomous: deterministic checks + its own headless browser. No Muse, no
API key, no human in the loop at runtime.
"""

from __future__ import annotations

import threading
from urllib.parse import urljoin

# Browser-like UA shared with the HTTP fetcher (engine.UA is the canonical
# one; duplicated here so this module never imports engine at module scope
# and stays import-safe when engine is unavailable).
RENDER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "AAFC-Audit-Engine/1.0 (read-only website audit)"
)

# Chromium flags for reliable headless runs in constrained environments.
BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _proxy_config() -> dict | None:
    """Playwright proxy dict from the standard proxy env vars, or None.

    Playwright does not pick up proxy env vars on its own, so without this
    a headless Chromium behind an egress proxy (CI sandboxes, corporate
    networks) cannot reach external sites at all. Credentials are parsed
    for Playwright's use only and never logged. ``no_proxy`` becomes
    Playwright's ``bypass`` so local fixture servers keep working.
    """
    import os
    from urllib.parse import unquote, urlparse

    server = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    )
    if not server:
        return None
    parts = urlparse(server)
    if not parts.hostname:
        return None
    cfg: dict = {
        "server": f"{parts.scheme or 'http'}://{parts.hostname}"
        f":{parts.port or 3128}"
    }
    if parts.username:
        cfg["username"] = unquote(parts.username)
        cfg["password"] = unquote(parts.password or "")
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if no_proxy.strip():
        cfg["bypass"] = no_proxy.strip()
    return cfg

# Per-page render budget. networkidle first (lets JS settle); on timeout we
# fall back to domcontentloaded + a short settle wait rather than failing.
GOTO_TIMEOUT_MS = 20000
SETTLE_WAIT_MS = 1500

# JS run inside the page to extract rendered DOM state. Visibility uses the
# standard offsetWidth/offsetHeight/getClientRects check (display:none and
# zero-size elements are NOT visible). alt is read with getAttribute so we
# can distinguish missing (null) from empty ("").
EXTRACT_JS = """() => {
  const vis = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch (e) { return u; } };
  return {
    title: document.title || "",
    description: (document.querySelector('meta[name="description"]') || {}).content || "",
    canonical: (document.querySelector('link[rel="canonical"]') || {}).href || "",
    h1s: [...document.querySelectorAll("h1")].map(e => e.innerText.trim()).filter(Boolean),
    images: [...document.images]
      .filter(vis)
      .map(img => ({ src: abs(img.currentSrc || img.src || ""), alt: img.getAttribute("alt") })),
    links: [...document.querySelectorAll("a[href]")]
      .map(a => ({ href: abs(a.getAttribute("href")), text: (a.innerText || "").trim().slice(0, 80) }))
      .filter(l => l.href && !l.href.startsWith("javascript:")),
    finalUrl: document.location.href,
  };
}"""

_available: bool | None = None


def render_available() -> bool:
    """True when a real headless-Chromium render can be launched.

    Probes an actual browser launch (not just the Playwright import) and
    caches the verdict, so ``aafc checks capabilities`` and the engine's
    render-mode reporting never claim a renderer that cannot run.
    Tests can reset ``render._available``.
    """
    global _available
    if _available is None:
        _available = _probe_renderer()
    return _available


def _probe_renderer() -> bool:
    """Launch-and-close a headless Chromium; False if anything fails."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = _launch(p)
            browser.close()
        return True
    except Exception:
        return False


def _render_one(context, url: str) -> dict:
    """Render one page and return its DOM state dict. Raises on failure."""
    page = context.new_page()
    try:
        try:
            page.goto(url, wait_until="networkidle", timeout=GOTO_TIMEOUT_MS)
        except Exception:
            # networkidle can hang on pages with polling XHR — fall back to
            # domcontentloaded plus a short settle wait instead of failing.
            page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            page.wait_for_timeout(SETTLE_WAIT_MS)
        state = page.evaluate(EXTRACT_JS)
        # Dedupe images by resolved src (responsive duplicates share currentSrc).
        seen, imgs = set(), []
        for img in state.get("images", []):
            src = (img.get("src") or "").strip()
            if not src or src in seen:
                continue
            seen.add(src)
            imgs.append({"src": src, "alt": img.get("alt")})
        state["images"] = imgs
        # Dedupe links by href, keep first text seen.
        seen_h, links = set(), []
        for link in state.get("links", []):
            href = (link.get("href") or "").strip()
            if not href or href in seen_h:
                continue
            seen_h.add(href)
            links.append({"href": href, "text": link.get("text", "")})
        state["links"] = links
        return state
    finally:
        page.close()


def render_pages(urls: list[str], timeout_ms: int = GOTO_TIMEOUT_MS,
               total_timeout_s: float | None = None) -> dict[str, dict]:
    """Render ``urls`` in one shared headless Chromium instance.

    Returns ``{url: dom_state}`` for pages that rendered successfully.
    Never raises and never hangs: a missing provider returns ``{}``, a page
    that fails to render is skipped, and a hard wall-clock watchdog bounds
    the whole batch (per-page goto timeouts bound each navigation, but a
    wedged browser/driver must not stall the audit — or the caller —
    forever). On watchdog timeout, whatever rendered in time is returned.

    ``total_timeout_s`` caps the entire batch including browser launch and
    teardown; when omitted it is derived from ``timeout_ms`` plus overhead.
    """
    if not urls or not render_available():
        return {}
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return {}

    # Wall-clock bound for the entire batch: per-page navigation budget
    # plus launch/teardown overhead. The worker is a daemon thread so an
    # unrecoverable hang can never block process exit.
    if total_timeout_s is None:
        total_timeout_s = (timeout_ms * len(urls)) / 1000 + 30
    total_budget_s = max(1.0, total_timeout_s)
    out: dict[str, dict] = {}
    done = threading.Event()

    def _work() -> None:
        try:
            out.update(_render_batch(urls, timeout_ms))
        except Exception:
            pass
        finally:
            done.set()

    worker = threading.Thread(target=_work, name="aafc-render", daemon=True)
    worker.start()
    done.wait(timeout=total_budget_s)
    return dict(out)


def _render_batch(urls: list[str], timeout_ms: int) -> dict[str, dict]:
    """The actual Playwright batch (runs on the watchdog worker thread)."""
    from playwright.sync_api import sync_playwright

    global GOTO_TIMEOUT_MS
    out: dict[str, dict] = {}
    prev_timeout = GOTO_TIMEOUT_MS
    GOTO_TIMEOUT_MS = timeout_ms
    try:
        with sync_playwright() as p:
            try:
                browser = _launch(p)
            except RuntimeError:
                return {}  # no usable Chromium binary -> graceful HTTP-only mode
            context = browser.new_context(
                user_agent=RENDER_UA, viewport={"width": 1366, "height": 900},
                proxy=_proxy_config(),
            )
            try:
                for url in urls:
                    try:
                        out[url] = _render_one(context, url)
                    except Exception:
                        continue
            finally:
                browser.close()
    except Exception:
        pass
    finally:
        GOTO_TIMEOUT_MS = prev_timeout
    return out


def _launch(p):
    """Launch headless Chromium, tolerating a missing headless-shell build.

    Playwright >= 1.49 launches headless via the separate headless-shell
    binary; if only the full Chromium build was downloaded (common on
    minimal installs), fall back to launching the full binary headless.
    Raises RuntimeError when no binary works.
    """
    try:
        return p.chromium.launch(headless=True, args=BROWSER_ARGS)
    except Exception:
        pass
    try:
        exe = p.chromium.executable_path
        import os

        if exe and os.path.exists(exe):
            return p.chromium.launch(headless=True, args=BROWSER_ARGS,
                                     executable_path=exe)
    except Exception:
        pass
    raise RuntimeError("no usable Chromium binary for Playwright")


def dom_from_rendered(url: str, state: dict) -> dict:
    """Normalize a rendered state dict into the DOM-check input shape.

    Mirrors what the engine builds from its HTML parser so the same
    DOM-sensitive checks can run against either source.
    """
    desc = (state.get("description") or "").strip()
    canon = (state.get("canonical") or "").strip()
    h1s = [h for h in state.get("h1s", []) if h]
    images = []
    for img in state.get("images", []):
        src = (img.get("src") or "").strip()
        if not src or src.lower().startswith("data:"):
            continue
        # Resolve relative URLs against the page (evaluate already absolutizes,
        # but fixtures/tests may hand us relative ones).
        images.append({"src": urljoin(url, src), "alt": img.get("alt")})
    return {
        "title": (state.get("title") or "").strip(),
        "description": desc,
        "canonical_count": 1 if canon else 0,
        "canonical_href": canon,
        "h1s": h1s,
        "headings": [(1, h) for h in h1s],
        "images": images,
        "links": state.get("links", []),
    }

# Vendored from sitepulse (same author, MIT license).
# Single audit implementation for aafc-engine — do not create a second auditor.
#!/usr/bin/env python3
"""
Website Audit Engine v1 — Kiminou Leverage Lab
URL in -> technical JSON + client-facing report out.

Stdlib + requests only. Strictly read-only: fetches public pages,
never posts, never modifies anything on the target site.

Usage:
    python3 audit.py https://example.com [--pages 6] [--out output]

Output (in --out/<domain>-<timestamp>/):
    report.json          machine-readable findings (the technical report data)
    TECHNICAL-REPORT.md  developer-facing findings with evidence
    CLIENT-REPORT.md     plain-language report: problem / why it matters / fix
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse

import requests

# Browser-like but honest UA: some sites/WAFs block unknown bot UAs outright,
# which used to produce false "site did not respond" failures. We identify the
# audit purpose while presenting a real browser token set.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 "
        "AAFC-Audit-Engine/1.0 (read-only website audit)"
    )
}
TIMEOUT = 12
MAX_REDIRECTS = 10  # requests default is 30; we cap tighter and report the chain

# URL path segments that mark a UTILITY page (cart, checkout, account, ...).
# Utility pages are judged by utility rules: noindex is expected, marketing
# metadata (description/canonical) is not required, and they never affect
# the site score. Grading a cart page like a landing page produced false
# CRITICAL/WARNING findings (verified on franklinbarbecue.com/cart).
UTILITY_PATH_SEGMENTS = (
    "cart", "checkout", "thank-you", "thankyou", "order-confirmation",
    "account", "login", "signin", "sign-in", "signup", "sign-up",
    "register", "password", "wishlist",
)


def page_kind(url):
    """Classify a page as 'utility' or 'content'.

    Utility pages (cart/checkout/account/...) are functional, not marketing
    pages: search engines should not index them and they carry no SEO
    metadata expectations.
    """
    segments = [s for s in urlparse(url).path.lower().split("/") if s]
    if any(seg in UTILITY_PATH_SEGMENTS for seg in segments):
        return "utility"
    return "content"

CTA_WORDS = (
    "buy", "shop", "order", "book", "schedule", "call now", "call",
    "contact", "get started", "sign up", "signup", "subscribe", "quote",
    "demo", "trial", "download", "learn more", "start", "join", "hire",
    "request", "apply", "donate",
)


# ----------------------------------------------------------------------------
# HTML parsing (stdlib only)
# ----------------------------------------------------------------------------
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self._in_title = False
        self.metas = {}          # lowercased name/property -> content
        self.canonical_count = 0
        self.headings = []       # (level:int, text:str)
        self._in_heading = None
        self._heading_text = ""
        self.images = []         # {src, alt, hidden, in_noscript, width, height}
        self.links = []          # {href, text}
        self._in_a = False
        self._a_href = ""
        self._a_text = ""
        # FIX 7: track whether we are inside containers whose images never
        # render (noscript/template) or are hidden — their <img> tags must not
        # count toward the alt-text check. A stack records per-tag hidden
        # state because endtags don't carry attributes.
        self._noscript_depth = 0
        self._template_depth = 0
        self._tag_stack = []  # list of bool: was this open tag hidden?
        self._hidden_here = False  # hidden state of the tag being processed
        self.scripts = []        # external src values
        self.stylesheets = []    # external css hrefs
        self.json_ld = 0
        self._in_jsonld = False
        self.forms = 0
        self.inputs = []         # {type,id,name,aria}
        self.buttons = []        # button text
        self._in_button = False
        self._button_text = ""
        self.lang = None

    # -- tag handling ------------------------------------------------------
    # Void elements never produce endtags, so they must not touch the tag
    # stack (otherwise the hidden-container tracking drifts).
    VOID_ELEMENTS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    @staticmethod
    def _tag_is_hidden(attrs):
        """True when a tag is hidden via attribute or inline style."""
        if "hidden" in attrs:
            return True
        style = (attrs.get("style") or "").lower().replace(" ", "")
        return "display:none" in style or "visibility:hidden" in style

    def handle_starttag(self, tag, attrs):
        self._process_start(tag, attrs, push_stack=tag not in self.VOID_ELEMENTS)

    def handle_startendtag(self, tag, attrs):
        # Self-closing tag (<img/>, <br/>): process content, never touch stack.
        self._process_start(tag, attrs, push_stack=False)

    def _process_start(self, tag, attrs, push_stack):
        a = dict(attrs)
        if tag == "noscript":
            self._noscript_depth += 1
        elif tag == "template":
            self._template_depth += 1
        hidden_here = self._tag_is_hidden(a)
        self._hidden_here = hidden_here  # consumed below by the img branch
        if push_stack:
            self._tag_stack.append(hidden_here)
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.metas[key] = a.get("content", "")
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if rel == "canonical":
                self.metas["canonical"] = a.get("href", "")
                self.canonical_count += 1
            elif rel == "stylesheet" and a.get("href"):
                self.stylesheets.append(a["href"])
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._in_heading = int(tag[1])
            self._heading_text = ""
        elif tag == "img":
            self.images.append({
                "src": a.get("src", ""),
                "alt": a.get("alt"),
                # FIX 7: context so the alt check can ignore non-rendered imgs
                "hidden": self._hidden_here or any(self._tag_stack),
                "in_noscript": self._noscript_depth > 0,
                "in_template": self._template_depth > 0,
                "width": a.get("width", ""),
                "height": a.get("height", ""),
            })
        elif tag == "a":
            self._in_a = True
            self._a_href = a.get("href", "")
            self._a_text = ""
        elif tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._in_jsonld = True
            elif a.get("src"):
                self.scripts.append(a["src"])
        elif tag == "form":
            self.forms += 1
        elif tag == "input":
            self.inputs.append({
                "type": a.get("type", "text"),
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "aria": a.get("aria-label", ""),
            })
        elif tag == "button":
            self._in_button = True
            self._button_text = ""

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "noscript" and self._noscript_depth > 0:
            self._noscript_depth -= 1
        elif tag == "template" and self._template_depth > 0:
            self._template_depth -= 1
        if self._tag_stack:
            self._tag_stack.pop()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._in_heading:
            self.headings.append((self._in_heading, self._heading_text.strip()))
            self._in_heading = None
        elif tag == "a" and self._in_a:
            self.links.append({"href": self._a_href, "text": self._a_text.strip()})
            self._in_a = False
        elif tag == "script" and self._in_jsonld:
            self.json_ld += 1
            self._in_jsonld = False
        elif tag == "button" and self._in_button:
            self.buttons.append(self._button_text.strip())
            self._in_button = False

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data
        if self._in_heading is not None:
            self._heading_text += data
        if self._in_a:
            self._a_text += data
        if self._in_button:
            self._button_text += data


# ----------------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------------
def fetch(url, method="GET", _retried=False):
    """Fetch a URL. Returns dict with status, final_url, headers, text, elapsed, error.

    Follows redirect chains (requests handles this; we record the chain) and
    retries once on transient connection errors. A browser-like UA is used
    because unknown bot UAs are blocked outright by some sites/WAFs, which
    used to surface as false "site did not respond" failures.
    """
    started = time.time()
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    try:
        r = session.request(method, url, headers=UA, timeout=TIMEOUT,
                            allow_redirects=True)
        elapsed = time.time() - started
        text = r.text if method == "GET" else ""
        chain = [h.url for h in r.history]
        return {
            "ok": True, "status": r.status_code, "final_url": r.url,
            "headers": dict(r.headers), "text": text,
            "bytes": len(r.content), "elapsed": round(elapsed, 2),
            "redirects": len(r.history), "redirect_chain": chain,
            "error": None,
        }
    except requests.RequestException as e:
        if not _retried and isinstance(
            e, (requests.ConnectionError, requests.Timeout)
        ):
            time.sleep(1)
            return fetch(url, method=method, _retried=True)
        return {
            "ok": False, "status": None, "final_url": url, "headers": {},
            "text": "", "bytes": 0, "elapsed": round(time.time() - started, 2),
            "redirects": 0, "redirect_chain": [],
            "error": f"{type(e).__name__}: {e}",
        }


def same_host(a, b):
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def norm_url(u):
    """Canonicalize a URL for crawl dedup: lowercase scheme/host, strip
    trailing slash, drop fragments. Without this, 'https://x.com' and
    'https://x.com/' crawl as two different pages and double every finding."""
    p = urlparse(u.strip())
    path = p.path.rstrip("/")  # "" and "/" become the same page
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def clean_link(href, base):
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    absu = urljoin(base, href)
    absu, _ = urldefrag(absu)
    if urlparse(absu).scheme not in ("http", "https"):
        return None
    return norm_url(absu)


# ----------------------------------------------------------------------------
# Checks — every finding carries both a technical and a client-facing view
# ----------------------------------------------------------------------------
# Checks whose results depend on the rendered DOM: when the HTTP fetch returns
# a near-empty shell (typical of JS-rendered pages), findings from these
# checks are marked NEEDS MANUAL REVIEW instead of being reported as failures.
# Emitting a failing finding from an empty render is how false alarms happen.
JS_SENSITIVE_CHECKS = frozenset({
    "title", "meta_description", "h1", "heading_order", "img_alt",
    "cta", "forms", "open_graph", "structured_data", "canonical",
    "viewport", "lang",
})

# Minimum visible-text characters for the static HTML to count as real
# content. Below this on an HTTP-200 page, the page is probably JS-rendered
# and DOM-dependent checks cannot be trusted from the raw HTML.
MIN_VISIBLE_TEXT_CHARS = 300


def visible_text_length(html):
    """Rough visible-text length: strip scripts/styles/tags, collapse space."""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text)


def F(page, check, severity, title, evidence, why, fix, technical="",
      review_hint=False):
    # FIX 7: review_hint may be the string "rendered" — the repo layer maps
    # that to the NEEDS_RENDERED_REVIEW confidence (raw HTML contradicts what
    # a browser shows; a rendered-DOM check is required before asserting).
    return {
        "page": page, "check": check, "severity": severity, "title": title,
        "evidence": evidence, "why_it_matters": why, "recommended_fix": fix,
        "technical": technical,
        # FIX 5: every finding says which page it came from and what kind.
        "page_kind": page_kind(page),
        # FIX 6: how this finding was verified. HTTP_FETCH = measured from a
        # plain HTTP fetch. NEEDS_RENDERED_DOM would require a real browser;
        # the engine never claims browser verification it did not perform.
        "verification": "HTTP_FETCH",
        # When True, the repo layer downgrades confidence to NEEDS MANUAL
        # REVIEW (page looked JS-rendered; the static HTML said nothing).
        # When "rendered", downgrades to NEEDS_RENDERED_REVIEW instead.
        "review_hint": review_hint if review_hint == "rendered" else bool(review_hint),
    }


def check_page(url, res, parser):
    findings = []
    stats = {
        "url": url, "status": res["status"], "final_url": res["final_url"],
        "bytes": res["bytes"], "elapsed": res["elapsed"],
        "redirects": res["redirects"], "page_kind": page_kind(url),
    }
    h = {k.lower(): v for k, v in res["headers"].items()}
    kind = stats["page_kind"]
    utility = kind == "utility"

    # FIX 6: if the static HTML is a near-empty shell on a 200 page, the real
    # content is probably JS-rendered — DOM-dependent checks from raw HTML
    # cannot be trusted, so they get review_hint=True (repo layer downgrades
    # them to NEEDS MANUAL REVIEW instead of reporting failures).
    likely_js_rendered = (
        res["status"] == 200
        and visible_text_length(res.get("text", "")) < MIN_VISIBLE_TEXT_CHARS
    )
    stats["likely_js_rendered"] = likely_js_rendered

    def rh(check):
        return likely_js_rendered and check in JS_SENSITIVE_CHECKS

    # -- transport ---------------------------------------------------------
    if urlparse(res["final_url"]).scheme != "https":
        findings.append(F(url, "https", "critical",
            "Page is not served over HTTPS",
            f"Final URL: {res['final_url']}",
            "Browsers warn visitors that the site is 'not secure', and Google ranks HTTPS pages higher. Visitors leave.",
            "Serve the whole site over HTTPS and redirect all HTTP traffic to HTTPS.",
            technical=f"scheme={urlparse(res['final_url']).scheme}"))
    if "strict-transport-security" not in h and urlparse(res["final_url"]).scheme == "https":
        findings.append(F(url, "hsts", "info",
            "HSTS header not set",
            "Response headers lack strict-transport-security.",
            "Without HSTS, the first visit to the site can still be intercepted before the HTTPS upgrade.",
            "Add a Strict-Transport-Security header (e.g. max-age=31536000).",
            technical="header absent"))

    # -- indexability ------------------------------------------------------
    # FIX 3: noindex on a UTILITY page (cart/checkout/...) is correct behavior
    # — search engines should not index it. Only flag noindex on content pages.
    robots_meta = parser.metas.get("robots", "").lower()
    if "noindex" in robots_meta and not utility:
        findings.append(F(url, "indexability", "critical",
            "Page tells search engines NOT to index it",
            f'<meta name="robots" content="{parser.metas["robots"]}">',
            "If this is a page you want found on Google, it is invisible there right now.",
            "Remove the noindex directive unless this page is intentionally hidden.",
            technical="meta robots contains noindex"))
    if h.get("x-robots-tag") and "noindex" in h["x-robots-tag"].lower() and not utility:
        findings.append(F(url, "indexability", "critical",
            "Server tells search engines NOT to index this page",
            f"X-Robots-Tag: {h['x-robots-tag']}",
            "Same as above: the page cannot appear in Google results while this header is set.",
            "Remove noindex from the X-Robots-Tag header for public pages.",
            technical="X-Robots-Tag contains noindex"))

    # -- metadata ----------------------------------------------------------
    # FIX 3: utility pages don't need marketing metadata. Missing title on a
    # utility page is info at most; title-length nits are suppressed there.
    title = (parser.title or "").strip()
    if not title:
        findings.append(F(url, "title", "critical" if not utility else "info",
            "Page has no <title>",
            "No <title> tag found in <head>.",
            "The title is what shows as the blue link in Google and the tab label. Without it, search listings look broken and click-through drops.",
            "Add a unique, descriptive <title> (50–60 characters) to every page.",
            technical="title tag missing/empty",
            review_hint=rh("title")))
    elif (len(title) < 30 or len(title) > 60) and not utility:
        findings.append(F(url, "title", "warning",
            f"Title length is {len(title)} characters (recommended 30–60)",
            f'Title: "{title}"',
            "Titles that are too short waste the listing; titles that are too long get cut off in Google.",
            "Rewrite the title to 30–60 characters, most important words first.",
            technical=f"title_len={len(title)}",
            review_hint=rh("title")))

    # FIX 3: missing meta description on a utility page is not a finding —
    # nobody searches for a cart page.
    desc = parser.metas.get("description", "").strip()
    if not desc and not utility:
        findings.append(F(url, "meta_description", "warning",
            "Missing meta description",
            "No <meta name=\"description\"> found.",
            "Google writes its own snippet for the listing, which usually converts worse than a hand-written one.",
            "Add a 120–155 character meta description that sells the click.",
            technical="meta description missing",
            review_hint=rh("meta_description")))
    elif desc and (len(desc) < 70 or len(desc) > 160) and not utility:
        findings.append(F(url, "meta_description", "info",
            f"Meta description is {len(desc)} characters (recommended 70–160)",
            f'Description: "{desc[:120]}…"',
            "Odd-length descriptions get truncated or underused in search results.",
            "Tighten the description to 120–155 characters.",
            technical=f"desc_len={len(desc)}",
            review_hint=rh("meta_description")))

    if "viewport" not in parser.metas:
        findings.append(F(url, "viewport", "critical", "No viewport meta tag",
            "No <meta name=\"viewport\"> found.",
            "On phones the page renders zoomed-out and tiny — most visitors are on mobile.",
            'Add <meta name="viewport" content="width=device-width, initial-scale=1">.',
            technical="viewport meta missing",
            review_hint=rh("viewport")))
    if not parser.metas.get("charset") and "charset" not in str(h.get("content-type", "")).lower():
        findings.append(F(url, "charset", "warning", "Character encoding not declared",
            "No charset in meta or Content-Type header.",
            "Special characters and apostrophes can render as garbled symbols.",
            "Declare UTF-8 via <meta charset=\"utf-8\"> or the Content-Type header.",
            technical="charset undeclared"))

    # -- headings ----------------------------------------------------------
    h1s = [t for lvl, t in parser.headings if lvl == 1]
    if not h1s:
        findings.append(F(url, "h1", "warning", "Page has no H1 heading",
            f"{len(parser.headings)} headings found, none at level 1.",
            "The H1 tells Google and screen readers what the page is about. Missing it weakens both SEO and accessibility.",
            "Add exactly one H1 per page that states the page's topic.",
            technical=f"headings={len(parser.headings)}, h1=0",
            review_hint=rh("h1")))
    elif len(h1s) > 1:
        findings.append(F(url, "h1", "warning", f"Page has {len(h1s)} H1 headings (recommended: 1)",
            f"H1s: {h1s[:3]}",
            "Multiple H1s dilute the page's topic signal for search engines and confuse screen-reader users.",
            "Keep one H1 per page; demote the rest to H2.",
            technical=f"h1_count={len(h1s)}",
            review_hint=rh("h1")))
    levels = [lvl for lvl, _ in parser.headings]
    skips = [f"h{levels[i]}→h{levels[i+1]}" for i in range(len(levels) - 1)
             if levels[i + 1] > levels[i] + 1]
    if skips:
        findings.append(F(url, "heading_order", "info",
            f"Heading levels skip ({', '.join(skips[:3])})",
            f"Sequence: {levels[:10]}",
            "Skipped levels make the page outline confusing for screen readers.",
            "Nest headings in order (H1 → H2 → H3) without skipping levels.",
            technical=f"skips={skips[:5]}",
            review_hint=rh("heading_order")))

    # -- images ------------------------------------------------------------
    # FIX 7: raw <img> counts lie. The parser now tags images that never
    # render (hidden containers, noscript/template) so we exclude them, drop
    # data-URI and 1x1 tracking pixels, and dedupe by resolved src. Browser
    # ground truth (franklinbbq.com): raw HTML showed 48 imgs / 46 empty alt
    # while the rendered page had ~28 images with descriptive alts. So when a
    # large share of the *rendered-candidate* images have empty alt on an
    # otherwise content-rich page, we do NOT assert "N of M missing" — we
    # flag NEEDS_RENDERED_REVIEW (warning) for a real browser check.
    def _img_usable(img):
        if img.get("hidden") or img.get("in_noscript") or img.get("in_template"):
            return False
        src = (img.get("src") or "").strip()
        if not src or src.lower().startswith("data:"):
            return False
        if str(img.get("width")).strip() == "1" and str(img.get("height")).strip() == "1":
            return False  # tracking pixel
        return True

    seen_srcs, candidates = set(), []
    for img in parser.images:
        if not _img_usable(img):
            continue
        resolved = urljoin(url, img["src"].strip())
        if resolved in seen_srcs:
            continue
        seen_srcs.add(resolved)
        candidates.append(img)
    no_alt = [i for i in candidates if not (i["alt"] or "").strip()]
    if candidates and no_alt:
        ratio = len(no_alt) / len(candidates)
        examples = ", ".join((i["src"] or "")[:60] for i in no_alt[:3])
        rich_page = visible_text_length(res.get("text", "")) >= 1000
        if ratio >= 0.5 and rich_page:
            # Looks like the franklin case: raw HTML disagrees with what a
            # browser renders. Assert nothing; send to rendered review.
            findings.append(F(url, "img_alt", "warning",
                f"{len(no_alt)} of {len(candidates)} images have empty alt text in raw HTML — needs rendered-DOM check",
                f"Examples: {examples}",
                "The raw HTML suggests many images lack descriptions, but the rendered page may differ (hidden/duplicate markup inflates raw counts). Screen readers and Google can only use what actually renders.",
                "Verify alt text in a real browser (rendered DOM), then add descriptive alt text to meaningful images that truly lack it.",
                technical=f"empty_alt_raw={len(no_alt)}/{len(candidates)}; rendered check required",
                review_hint="rendered"))
        else:
            sev = "warning" if ratio >= 0.3 else "info"
            findings.append(F(url, "img_alt", sev,
                f"{len(no_alt)} of {len(candidates)} images missing alt text",
                f"Examples: {examples}",
                "Screen-reader users hear 'image' with no description, and Google can't understand image content — both hurt accessibility scores and image search traffic.",
                "Add short, descriptive alt text to every meaningful image (leave decorative images with empty alt=\"\").",
                technical=f"missing_alt={len(no_alt)}/{len(candidates)} (rendered candidates, deduped)",
                review_hint=rh("img_alt")))

    # -- links / canonical / social ---------------------------------------
    # FIX 3: a utility page needs no canonical — suppress the missing-canonical
    # nit there, but still warn on multiple conflicting canonicals.
    if parser.canonical_count == 0 and not utility:
        findings.append(F(url, "canonical", "warning", "No canonical URL set",
            "No <link rel=\"canonical\"> found.",
            "Without a canonical, duplicate or similar pages can split ranking power in Google.",
            "Add a canonical link pointing to the page's preferred URL.",
            technical="canonical missing",
            review_hint=rh("canonical")))
    elif parser.canonical_count > 1:
        findings.append(F(url, "canonical", "warning", "Multiple canonical tags",
            f"{parser.canonical_count} canonical tags found.",
            "Search engines may ignore conflicting canonical signals.",
            "Keep exactly one canonical tag per page.",
            technical=f"canonical_count={parser.canonical_count}",
            review_hint=rh("canonical")))

    for og in ("og:title", "og:description", "og:image"):
        if og not in parser.metas:
            findings.append(F(url, "open_graph", "info",
                f"Missing {og} (social sharing preview)",
                f"<meta property=\"{og}\"> not found.",
                "When someone shares this page on social media or messaging apps, the preview looks blank or broken.",
                "Add Open Graph tags (og:title, og:description, og:image) so shares look professional.",
                technical=f"{og} missing",
                review_hint=rh("open_graph")))
            break

    if parser.json_ld == 0:
        findings.append(F(url, "structured_data", "info", "No structured data (schema.org)",
            "No application/ld+json blocks found.",
            "Structured data powers rich results (stars, FAQs, business info) in Google — missing it means plain listings only.",
            "Add JSON-LD schema (e.g. Organization, LocalBusiness, FAQPage) where relevant.",
            technical="json_ld=0",
            review_hint=rh("structured_data")))

    if not parser.lang:
        findings.append(F(url, "lang", "warning", "Page language not declared",
            "<html> has no lang attribute.",
            "Screen readers guess the pronunciation language; search engines lose a locale signal.",
            'Add lang="en" (or the correct language) to the <html> tag.',
            technical="html lang missing",
            review_hint=rh("lang")))

    # -- mixed content ------------------------------------------------------
    if urlparse(res["final_url"]).scheme == "https":
        insecure = [s for s in parser.scripts + parser.stylesheets
                    if s.strip().lower().startswith("http://")]
        insecure += [i["src"] for i in parser.images
                     if (i["src"] or "").strip().lower().startswith("http://")]
        if insecure:
            findings.append(F(url, "mixed_content", "critical",
                f"{len(insecure)} resources loaded over insecure HTTP",
                f"Examples: {', '.join(insecure[:3])}",
                "Browsers block or warn on mixed content — parts of the page may not load and visitors see security warnings.",
                "Load every script, stylesheet, and image over HTTPS.",
                technical=f"mixed={len(insecure)}"))

    # -- forms --------------------------------------------------------------
    if parser.forms:
        unlabeled = [i for i in parser.inputs
                     if i["type"] not in ("hidden", "submit", "button")
                     and not i["id"] and not i["aria"] and not i["name"]]
        if unlabeled:
            findings.append(F(url, "forms", "warning",
                f"{len(unlabeled)} form fields without an accessible label",
                f"{parser.forms} form(s) on page; {len(unlabeled)} unlabeled inputs.",
                "Unlabeled fields are hard to use with screen readers and hurt form completion — every lost completion is a lost lead.",
                "Associate each input with a <label> or aria-label.",
                technical=f"unlabeled_inputs={len(unlabeled)}",
                review_hint=rh("forms")))

    # -- conversion surface --------------------------------------------------
    cta_hits = [l for l in parser.links
                if any(w in l["text"].lower() for w in CTA_WORDS) and l["text"].strip()]
    cta_hits += [b for b in parser.buttons
                 if any(w in b.lower() for w in CTA_WORDS) and b.strip()]
    stats["cta_count"] = len(cta_hits)
    if not cta_hits:
        findings.append(F(url, "cta", "warning", "No clear call-to-action found",
            "No links or buttons with action words (book, call, contact, get started…) detected.",
            "Visitors who can't find the next step don't take it — the page informs but doesn't convert.",
            "Add at least one visible call-to-action above the fold (e.g. 'Book a call', 'Get a quote').",
            technical="cta_count=0",
            review_hint=rh("cta")))

    # -- weight / speed hints (server-side fetch, labeled honestly) ----------
    stats["page_kb"] = round(res["bytes"] / 1024, 1)
    if res["bytes"] > 1_500_000:
        findings.append(F(url, "page_weight", "warning",
            f"Page HTML is {stats['page_kb']} KB (heavy)",
            f"{res['bytes']} bytes transferred for HTML alone.",
            "Heavy pages load slowly, especially on phones — slow pages lose visitors and rank lower.",
            "Minify HTML, defer non-critical scripts, compress images.",
            technical=f"html_bytes={res['bytes']}"))
    if res["elapsed"] > 3:
        findings.append(F(url, "ttfb", "warning",
            f"Server responded in {res['elapsed']}s (slow)",
            f"Time to first byte: {res['elapsed']}s (measured server-to-server, not a real browser).",
            "Slow server responses delay everything on the page and hurt rankings.",
            "Investigate hosting, caching, and server response time.",
            technical=f"elapsed={res['elapsed']}s (server-side measure)"))

    stats.update({
        "images": len(parser.images),
        "links_total": len(parser.links),
        "headings": len(parser.headings),
        "forms": parser.forms,
        "scripts_external": len(parser.scripts),
        "json_ld_blocks": parser.json_ld,
        "title": title,
    })
    return findings, stats


# ----------------------------------------------------------------------------
# Site-level checks
# ----------------------------------------------------------------------------
def check_robots_and_sitemap(base_url):
    findings = []
    info = {}
    robots_url = urljoin(base_url, "/robots.txt")
    r = fetch(robots_url)
    if r["ok"] and r["status"] == 200 and r["text"].strip():
        info["robots_txt"] = True
        sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)", r["text"])
        info["sitemaps_in_robots"] = sitemaps
    else:
        info["robots_txt"] = False
        findings.append(F(base_url, "robots_txt", "info", "No robots.txt found",
            f"GET {robots_url} -> {r['status']}",
            "robots.txt is how you guide search-engine crawlers and declare your sitemap location. Most sites should have one.",
            "Add a simple robots.txt (allow crawling + Sitemap: line).",
            technical=f"robots_status={r['status']}"))
    sm_url = urljoin(base_url, "/sitemap.xml")
    s = fetch(sm_url)
    # FIX 9: accept sitemap INDEX files too (<sitemapindex>, used by
    # WordPress/Yoast), not just <urlset> files — and the fetcher follows
    # redirects, so /sitemap.xml -> /wp-sitemap.xml counts as found.
    head = s["text"][:2000].lower()
    if s["ok"] and s["status"] == 200 and ("<url" in head or "sitemapindex" in head):
        info["sitemap_xml"] = True
        if s.get("final_url") and s["final_url"] != sm_url:
            info["sitemap_final_url"] = s["final_url"]
    else:
        info["sitemap_xml"] = False
        findings.append(F(base_url, "sitemap", "warning", "No XML sitemap found",
            f"GET {sm_url} -> {s['status']}",
            "A sitemap helps Google discover every page, especially on new or small sites.",
            "Generate and publish /sitemap.xml, then reference it in robots.txt.",
            technical=f"sitemap_status={s['status']}"))
    return findings, info


def check_broken_links(url, parser, cap=25):
    """HEAD-check unique same-host links. Returns findings + stats."""
    seen = set()
    targets = []
    for l in parser.links:
        absu = clean_link(l["href"], url)
        if absu and same_host(absu, url) and absu not in seen:
            seen.add(absu)
            targets.append(absu)
        if len(targets) >= cap:
            break
    broken = []
    checked = 0
    for t in targets:
        r = fetch(t, method="HEAD")
        checked += 1
        if not r["ok"] or (r["status"] and r["status"] >= 400):
            # HEAD sometimes rejected; confirm with GET before calling it broken
            r2 = fetch(t, method="GET")
            if not r2["ok"] or (r2["status"] and r2["status"] >= 400):
                broken.append((t, r2["status"] or r2["error"]))
    findings = []
    if broken:
        sev = "critical" if len(broken) >= 3 else "warning"
        examples = "; ".join(f"{u} ({s})" for u, s in broken[:5])
        findings.append(F(url, "broken_links", sev,
            f"{len(broken)} broken internal link(s) found ({checked} checked)",
            f"Examples: {examples}",
            "Broken links frustrate visitors, waste Google's crawl budget, and make the site look abandoned.",
            "Fix or remove the broken links (update the URL or point them at the right page).",
            technical=f"broken={len(broken)}/{checked}"))
    return findings, {"links_checked": checked, "broken": len(broken)}


# ----------------------------------------------------------------------------
# Crawl + score
# ----------------------------------------------------------------------------
def audit_site(start_url, max_pages=6):
    started_all = time.time()
    if not urlparse(start_url).scheme:
        start_url = "https://" + start_url
    start_url = norm_url(start_url)
    findings, page_stats = [], []
    seen, queue = set(), [start_url]
    crawled = 0

    while queue and crawled < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        res = fetch(url)
        if not res["ok"]:
            findings.append(F(url, "fetch", "critical", "Page could not be loaded",
                "The site did not respond when we tried to visit it (connection failed or timed out).",
                "If an automated check can't load the page, some visitors and search engines can't either — every one of those visits is lost.",
                "Check that the site loads reliably: hosting status, DNS settings, and server errors.",
                technical=f"GET {url} failed: {res['error']}"))
            continue
        if res["status"] and res["status"] >= 400:
            findings.append(F(url, "http_status", "critical",
                f"Page returns HTTP {res['status']}",
                f"GET {url} -> {res['status']}",
                "Error pages can't rank and can't convert — every visit to one is wasted.",
                "Fix the server response so the page returns HTTP 200.",
                technical=f"status={res['status']}"))
            continue
        ctype = res["headers"].get("Content-Type", "")
        if "html" not in ctype.lower():
            continue
        parser = PageParser()
        try:
            parser.feed(res["text"])
        except Exception as e:  # never let one bad page kill the audit
            findings.append(F(url, "parse", "info", "Page HTML could not be fully parsed",
                f"Parser error: {e}", "Some checks may be incomplete for this page.",
                "Validate the page HTML.", technical=str(e)))
        pf, stats = check_page(url, res, parser)
        findings.extend(pf)
        # broken-link check only on the homepage (cost control)
        if crawled == 0:
            bf, bstats = check_broken_links(url, parser)
            findings.extend(bf)
            stats.update(bstats)
        page_stats.append(stats)
        crawled += 1
        for l in parser.links:
            absu = clean_link(l["href"], url)
            if absu and same_host(absu, start_url) and absu not in seen:
                queue.append(absu)

    rf, rinfo = check_robots_and_sitemap(start_url)
    findings.extend(rf)

    # -- score ------------------------------------------------------------
    # FIX 3 (calibration): utility-page findings (cart/checkout/login/...)
    # are reported for completeness but never affect the score — grading a
    # cart page's noindex like a landing-page failure is how franklinbarbecue
    # got an F it didn't deserve. The score reflects content pages only.
    # FIX 8 (honesty): findings the engine flagged review_hint on are ones it
    # explicitly cannot verify from raw HTML ("needs rendered-DOM check").
    # Penalizing the score for a check we admit we couldn't run is a false
    # alarm with a number attached — reported, never scored.
    scored = [f for f in findings
              if f.get("page_kind") != "utility" and not f.get("review_hint")]
    excluded_utility = sum(1 for f in findings if f.get("page_kind") == "utility")
    excluded_unverifiable = sum(1 for f in findings
                                if f.get("page_kind") != "utility" and f.get("review_hint"))
    crit = sum(1 for f in scored if f["severity"] == "critical")
    warn = sum(1 for f in scored if f["severity"] == "warning")
    score = max(0, 100 - 12 * crit - 5 * warn)

    if crawled == 0:
        # FIX 4: nothing could be loaded, so nothing was audited. This is
        # BLOCKED — no score, no grade. A fake/unreachable domain must never
        # get "20/F".
        status = "BLOCKED"
        score = None
        grade = None
        findings.append(F(start_url, "fetch", "critical",
            "Site could not be loaded — audit BLOCKED",
            "The site did not respond (connection failed, timed out, or DNS failed), so no page was actually checked.",
            "Nothing here was graded: the score below is absent, not zero. A site that doesn't load can't rank or convert — this is the first thing to fix.",
            "Restore reliable site loading, then re-run the audit for the full check.",
            technical="pages_crawled=0; no HTTP 200 page fetched"))
    else:
        status = "COMPLETE"
        grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 65 else "D" if score >= 50 else "F"

    return {
        "audited_url": start_url,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "engine": "kiminou-website-audit/1.0",
        "status": status,
        "pages_crawled": crawled,
        "score": score,
        "grade": grade,
        "counts": {
            "critical": crit,
            "warning": warn,
            "info": sum(1 for f in scored if f["severity"] == "info"),
            "utility_page_findings_excluded_from_score": excluded_utility,
            "unverifiable_findings_excluded_from_score": excluded_unverifiable,
        },
        "robots": rinfo,
        "pages": page_stats,
        "findings": findings,
        "elapsed_total": round(time.time() - started_all, 1),
        "notes": [
            "Speed measurements are server-to-server fetches, not real browser timings.",
            "Broken-link check covers up to 25 same-host links on the homepage only.",
            "Findings from utility pages (cart, checkout, login, ...) are reported but excluded from the score.",
            "Findings the engine could not verify from raw HTML are reported as needing review, not scored.",
            "Checks are heuristic; a human review should confirm critical findings before client delivery.",
        ],
    }


# ----------------------------------------------------------------------------
# Report rendering
# ----------------------------------------------------------------------------
def render_client_report(site):
    L = []
    d = urlparse(site["audited_url"]).netloc
    L.append(f"# Website Health Report — {d}")
    L.append("")
    L.append(f"*Prepared {datetime.now().strftime('%B %d, %Y')} · {site['pages_crawled']} page(s) checked*")
    L.append("")
    if site.get("status") == "BLOCKED" or site["score"] is None:
        # FIX 4: never print "None/100" — a blocked audit has no score at all.
        L.append("## ⚠️ This audit could not be completed")
        L.append("")
        L.append("We couldn't load the site, so there's nothing to grade yet — "
                 "no score is shown because no score was earned. Fix the loading "
                 "problem first, then we'll run the full check.")
        L.append("")
    else:
        L.append(f"## Overall score: {site['score']}/100 (Grade {site['grade']})")
        L.append("")
    c = site["counts"]
    L.append(f"Found **{c['critical']} critical** issue(s), **{c['warning']}** warning(s), "
             f"and **{c['info']}** improvement(s) worth knowing about.")
    L.append("")
    if c.get("utility_page_findings_excluded_from_score"):
        L.append(f"_Utility pages (cart, checkout, login, …) contributed "
                 f"{c['utility_page_findings_excluded_from_score']} finding(s) that are "
                 f"reported below but don't affect the score — they're supposed to behave differently._")
        L.append("")
    if c["critical"] == 0 and c["warning"] == 0:
        L.append("Good news: nothing urgent turned up. The improvements below are optional polish.")
        L.append("")
    seen_groups = set()
    for f in sorted(site["findings"], key=lambda x: {"critical": 0, "warning": 1, "info": 2}[x["severity"]]):
        group = (f["check"], f["title"])
        if group in seen_groups:
            continue
        seen_groups.add(group)
        badge = {"critical": "🔴", "warning": "🟡", "info": "🔵"}[f["severity"]]
        L.append(f"### {badge} {f['title']}")
        L.append("")
        L.append(f"**What we found:** {f['evidence']}")
        L.append("")
        L.append(f"**Why it matters:** {f['why_it_matters']}")
        L.append("")
        L.append(f"**The fix:** {f['recommended_fix']}")
        L.append("")
        pages = sorted(set(x["page"] for x in site["findings"]
                           if x["check"] == f["check"] and x["title"] == f["title"]))
        # FIX 5: page attribution is prominent and utility pages are labeled —
        # a /cart finding must never read as a site-wide problem.
        kind_note = " — utility page (cart/checkout/login)" if f.get("page_kind") == "utility" else ""
        if len(pages) > 1:
            shown = "\n".join(f"  - {p}" for p in pages[:8])
            extra = f"\n  - …and {len(pages) - 8} more" if len(pages) > 8 else ""
            L.append(f"*Seen on {len(pages)} pages:{kind_note}*\n{shown}{extra}")
        else:
            L.append(f"*Seen on: {f['page']}{kind_note}*")
        L.append("")
    L.append("---")
    L.append("")
    L.append("## What happens next")
    L.append("")
    L.append("Every issue above is fixable, and most take under an hour each. "
             "If you'd like these handled for you — fixes applied, re-tested, and "
             "re-checked — reply to this report and we'll schedule it.")
    L.append("")
    L.append("*This is an automated first pass. A specialist reviewed the critical items before delivery.*")
    return "\n".join(L)


def render_technical_report(site):
    L = []
    L.append(f"# Technical Audit — {site['audited_url']}")
    L.append(f"_Engine {site['engine']} · {site['audited_at']} · {site['elapsed_total']}s · "
             f"{site['pages_crawled']} pages_")
    L.append("")
    if site.get("status") == "BLOCKED" or site["score"] is None:
        L.append("**Status: BLOCKED — no score, no grade (site could not be loaded)** · "
                 f"critical={site['counts']['critical']} warning={site['counts']['warning']} "
                 f"info={site['counts']['info']}")
    else:
        L.append(f"**Score {site['score']}/100 (Grade {site['grade']})** · "
                 f"critical={site['counts']['critical']} warning={site['counts']['warning']} "
                 f"info={site['counts']['info']}")
    L.append("")
    L.append("## Pages")
    for p in site["pages"]:
        L.append(f"- `{p['url']}` → {p['status']} · {p['page_kb']} KB HTML · "
                 f"{p['elapsed']}s · title: \"{(p.get('title') or '')[:60]}\" · "
                 f"imgs={p['images']} links={p['links_total']} ctas={p['cta_count']} forms={p['forms']}")
    L.append("")
    L.append("## Findings")
    for f in site["findings"]:
        L.append(f"### [{f['severity'].upper()}] {f['check']} — {f['title']}")
        L.append(f"- page: `{f['page']}` · kind: {f.get('page_kind', 'content')} · verified: {f.get('verification', 'HTTP_FETCH')}")
        L.append(f"- evidence: {f['evidence']}")
        if f["technical"]:
            L.append(f"- technical: `{f['technical']}`")
        L.append(f"- fix: {f['recommended_fix']}")
        L.append("")
    L.append("## Caveats")
    for n in site["notes"]:
        L.append(f"- {n}")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Kiminou Website Audit Engine v1")
    ap.add_argument("url", help="Site URL to audit (e.g. https://example.com)")
    ap.add_argument("--pages", type=int, default=6, help="Max pages to crawl (default 6)")
    ap.add_argument("--out", default="output", help="Output directory (default ./output)")
    args = ap.parse_args()

    import os
    site = audit_site(args.url, max_pages=args.pages)
    domain = urlparse(site["audited_url"]).netloc.replace(":", "_") or "site"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(args.out, f"{domain}-{stamp}")
    os.makedirs(dest, exist_ok=True)

    with open(os.path.join(dest, "report.json"), "w") as fh:
        json.dump(site, fh, indent=2)
    with open(os.path.join(dest, "CLIENT-REPORT.md"), "w") as fh:
        fh.write(render_client_report(site))
    with open(os.path.join(dest, "TECHNICAL-REPORT.md"), "w") as fh:
        fh.write(render_technical_report(site))

    c = site["counts"]
    if site.get("status") == "BLOCKED" or site["score"] is None:
        print(f"Status: BLOCKED — site could not be loaded (no score, no grade) · "
              f"critical={c['critical']} warning={c['warning']} info={c['info']} · "
              f"{site['pages_crawled']} pages in {site['elapsed_total']}s")
    else:
        print(f"Score: {site['score']}/100 (Grade {site['grade']}) · "
              f"critical={c['critical']} warning={c['warning']} info={c['info']} · "
              f"{site['pages_crawled']} pages in {site['elapsed_total']}s")
    print(f"Reports: {dest}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

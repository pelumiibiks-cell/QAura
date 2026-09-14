"""Bot-protection detection.

This module detects challenge pages and stops. It does not try to get past them: no user
agent spoofing, no stealth patching, no fingerprint evasion. If a site has decided not to
serve automated clients, working around that is not something qaura should do.

The failure it exists to prevent is subtler than "the crawl was blocked". Without this,
recon would treat a Cloudflare interstitial as an ordinary page: fingerprint it, scan it
for numbers, infer guardrails from it, and emit a confident-looking config describing a
challenge screen. Writing nothing is strictly better than that.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Response headers that name the protection product outright.
_VENDOR_HEADERS = {
    "cf-ray": "Cloudflare",
    "cf-mitigated": "Cloudflare",
    "x-akamai-request-id": "Akamai",
    "akamai-grn": "Akamai",
    "x-iinfo": "Imperva/Incapsula",
    "x-cdn": "Imperva/Incapsula",
    "x-sucuri-id": "Sucuri",
    "x-datadome": "DataDome",
    "x-datadome-cid": "DataDome",
}

# Body markers, checked only alongside a blocking status so an ordinary page that happens
# to contain the words "access denied" doesn't trip the detector.
_BODY_MARKERS = (
    "just a moment",
    "checking your browser",
    "enable javascript and cookies to continue",
    "attention required",
    "access denied",
    "request blocked",
    "you have been blocked",
    "why have i been blocked",
    "ray id",
    "cf-browser-verification",
    "ddos protection by",
    "verifying you are human",
    "please verify you are a human",
    "incident id",
    "reference #",
    "pardon our interruption",
    "unusual traffic from your computer",
)

_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "security check",
    "are you a robot",
    "bot verification",
    "please wait",
    "ddos-guard",
)

_CHALLENGE_STATUSES = frozenset({403, 429, 503})

# Present on the page itself rather than in the response, so worth checking even on a 200
# — Cloudflare's managed challenge serves a 200 with the interstitial inline.
_CHALLENGE_SELECTOR_MARKERS = (
    "#cf-challenge-running",
    "#challenge-form",
    "#challenge-running",
    "div.cf-browser-verification",
    "#px-captcha",
    "iframe[src*='captcha']",
    "iframe[title*='challenge']",
)


@dataclass
class ChallengeSignal:
    detected: bool = False
    vendor: str | None = None
    status: int | None = None
    evidence: list[str] = field(default_factory=list)

    def describe(self) -> str:
        who = self.vendor or "bot protection"
        status = f" (HTTP {self.status})" if self.status else ""
        return f"{who}{status}: " + "; ".join(self.evidence)


def classify_challenge(
    *,
    status: int | None,
    headers: dict[str, str] | None,
    title: str,
    body_text: str,
    matched_selectors: list[str] | None = None,
) -> ChallengeSignal:
    """Pure classifier, so the whole policy is testable without a browser or a live WAF."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    title_l = (title or "").lower()
    body_l = (body_text or "").lower()
    matched = matched_selectors or []

    vendor = next((name for h, name in _VENDOR_HEADERS.items() if h in headers), None)
    evidence: list[str] = []

    blocking_status = status in _CHALLENGE_STATUSES if status is not None else False
    title_hit = next((m for m in _TITLE_MARKERS if m in title_l), None)
    body_hit = next((m for m in _BODY_MARKERS if m in body_l), None)

    if matched:
        evidence.append(f"challenge element present ({matched[0]})")
    if title_hit:
        evidence.append(f"page title matches {title_hit!r}")
    if blocking_status and body_hit:
        evidence.append(f"HTTP {status} body matches {body_hit!r}")
    if vendor and blocking_status:
        evidence.append(f"{vendor} header on an HTTP {status} response")

    # A vendor header alone proves nothing — most of the internet is behind a CDN, and
    # cf-ray is on every Cloudflare-fronted 200. It only counts as corroboration.
    detected = bool(matched or title_hit or (blocking_status and (body_hit or vendor)))
    if detected and vendor and not any(vendor in e for e in evidence):
        evidence.append(f"served via {vendor}")

    return ChallengeSignal(
        detected=detected,
        vendor=vendor,
        status=status,
        evidence=evidence or (["blocked response"] if detected else []),
    )


async def detect_challenge_page(page, response) -> ChallengeSignal:
    """Live wrapper over classify_challenge()."""
    status = None
    headers: dict[str, str] = {}
    if response is not None:
        try:
            status = response.status
            headers = await response.all_headers()
        except Exception:
            headers = {}

    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body_text = (await page.locator("body").text_content() or "")[:4000]
    except Exception:
        body_text = ""

    matched: list[str] = []
    for selector in _CHALLENGE_SELECTOR_MARKERS:
        try:
            if await page.locator(selector).count() > 0:
                matched.append(selector)
                break
        except Exception:
            continue

    return classify_challenge(
        status=status, headers=headers, title=title, body_text=body_text, matched_selectors=matched
    )

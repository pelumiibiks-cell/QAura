"""The read-only recon crawl that everything else in `qaura init` is derived from.

A separate crawler from core/heuristic.py, deliberately. That one exists to find bugs: it
fuzzes inputs, submits forms, runs every detector, and paces itself with
RunLimiter.throttle(), which calls a blocking time.sleep() inside async code. Recon wants
none of that — it observes and leaves. Configuring the existing crawler into harmlessness
would mean threading "don't actually do your job" flags through several modules and
trusting every future change to respect them; a purpose-built loop that simply never
calls those paths is both smaller and honest about what it guarantees.

What it does reuse is everything that isn't about mutation: guard_goto for scope,
StateGraph/fingerprint for dedupe, build_page_model for observation, and
scan_numeric_elements for the inventory.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.async_api import Page
from rich.console import Console

from qaura.browser.auth import DEFAULT_AUTH_DIR
from qaura.browser.driver import ContextSpec, Driver
from qaura.browser.numerics import NumericElement, scan_numeric_elements
from qaura.browser.observe import PageModel, build_page_model
from qaura.browser.readonly import AllowOnce, ReadOnlyLedger, install_readonly_routes
from qaura.config import GuardrailConfig
from qaura.core.guardrails import GuardrailViolation, guard_goto, is_destructive, is_in_scope
from qaura.core.state import StateGraph, fingerprint, url_template
from qaura.init.challenge import ChallengeSignal, detect_challenge_page
from qaura.init.limits import Pacer, ReconLimits
from qaura.init.login import LoginWallSignal, attempt_login, detect_login_wall, summarize_wall

_log = logging.getLogger(__name__)

# Records client-side route changes that never touch an <a href>. Without this a SPA
# looks like a one-page site: its router swaps views via history.pushState and the DOM
# contains no link the crawler could follow.
_HISTORY_HOOK_JS = """
(() => {
  if (window.__qauraRoutes) return;
  window.__qauraRoutes = [];
  const record = () => { try { window.__qauraRoutes.push(location.href); } catch (e) {} };
  for (const name of ['pushState', 'replaceState']) {
    const original = history[name];
    history[name] = function (...args) {
      const result = original.apply(this, args);
      record();
      return result;
    };
  }
  window.addEventListener('popstate', record);
})();
"""


class ChallengeDetected(RuntimeError):
    """Bot protection was found. Recon stops rather than describing a challenge page."""

    def __init__(self, signal: ChallengeSignal) -> None:
        self.signal = signal
        super().__init__(signal.describe())


@dataclass
class Snapshot:
    """One captured page state, replayed offline by the invariant validator."""

    url: str
    html: str
    viewport: tuple[int, int]
    load_index: int


@dataclass
class ObservedState:
    key: str
    url: str
    template: str
    title: str
    status: int | None
    depth: int
    model: PageModel
    numerics: list[NumericElement]
    login_signal: LoginWallSignal
    html: str


@dataclass
class ReconResult:
    seed_url: str
    final_url: str
    states: list[ObservedState] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    graph: StateGraph = field(default_factory=StateGraph)
    discovered_paths: list[str] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)
    nav_latencies_ms: list[float] = field(default_factory=list)
    saw_429: bool = False
    rate_limit_headers: bool = False
    ledger: ReadOnlyLedger = field(default_factory=ReadOnlyLedger)
    login: LoginWallSignal = field(default_factory=LoginWallSignal)
    role: str | None = None
    attempted: int = 0
    route_sources: dict[str, int] = field(default_factory=dict)
    robots_disallowed: list[str] = field(default_factory=list)
    external_hosts: set[str] = field(default_factory=set)
    subdomains_visited: set[str] = field(default_factory=set)
    admin_measured: dict[str, str] = field(default_factory=dict)
    assisted_login: str | None = None
    truncated_reason: str | None = None
    max_depth: int = 0

    @property
    def median_latency_ms(self) -> float:
        if not self.nav_latencies_ms:
            return 0.0
        ordered = sorted(self.nav_latencies_ms)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2


# --- robots.txt and sitemap.xml -----------------------------------------------------


def parse_robots(text: str) -> tuple[list[str], list[str]]:
    """Returns (disallowed path patterns for *, sitemap URLs).

    Only the `*` user-agent group is honored. A group targeting a named bot says nothing
    about what a generic client may fetch, and treating it as if it did would make recon
    stricter than the site actually asks for.
    """
    disallowed: list[str] = []
    sitemaps: list[str] = []
    applies = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()
        if field_name == "user-agent":
            applies = value == "*"
        elif field_name == "sitemap" and value:
            sitemaps.append(value)
        elif field_name == "disallow" and applies and value:
            disallowed.append(value)
    return disallowed, sitemaps


def parse_sitemap(text: str) -> list[str]:
    """Deliberately regex-based rather than an XML parse: sitemaps in the wild are often
    slightly malformed, and a strict parser that raises on the whole document loses every
    URL in it over one bad entry."""
    import re

    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text, re.IGNORECASE)


def robots_blocks(path: str, disallowed: list[str]) -> bool:
    for pattern in disallowed:
        if pattern == "/":
            return True
        prefix = pattern.rstrip("*")
        if path.startswith(prefix):
            return True
    return False


async def fetch_route_hints(
    seed_url: str, cfg: GuardrailConfig, *, respect_robots: bool, timeout: float = 8.0
) -> tuple[list[str], list[str]]:
    """Seeds the frontier from robots.txt and sitemap.xml. Two plain GETs that routinely
    surface more of a server-rendered site than link-following does, and the robots fetch
    is needed anyway to know what the site asks crawlers not to touch."""
    parts = urlsplit(seed_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    urls: list[str] = []
    disallowed: list[str] = []

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        sitemap_urls = [urljoin(origin, "/sitemap.xml")]
        try:
            resp = await client.get(urljoin(origin, "/robots.txt"))
            if resp.status_code == 200 and "text" in resp.headers.get("content-type", "text"):
                disallowed, declared = parse_robots(resp.text)
                sitemap_urls = declared or sitemap_urls
        except Exception:
            _log.debug("robots.txt fetch failed for %s", origin, exc_info=True)

        for sitemap_url in sitemap_urls[:3]:
            try:
                resp = await client.get(sitemap_url)
                if resp.status_code != 200:
                    continue
                for loc in parse_sitemap(resp.text)[:200]:
                    if not is_in_scope(loc, cfg):
                        continue
                    if respect_robots and robots_blocks(urlsplit(loc).path or "/", disallowed):
                        continue
                    urls.append(loc)
            except Exception:
                _log.debug("sitemap fetch failed for %s", sitemap_url, exc_info=True)

    return urls, disallowed


# --- the crawl ----------------------------------------------------------------------


async def _settle(page: Page, limits: ReconLimits) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=limits.settle_timeout_ms)
    except Exception:
        # A page with a long-poll or an open websocket never goes idle. Not a problem —
        # domcontentloaded already fired, so the DOM is there to observe.
        _log.debug("networkidle wait timed out on %s", page.url)


async def _collect_links(page: Page) -> list[str]:
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href).filter(Boolean)"
        )
    except Exception:
        return []
    return [h for h in hrefs if h.startswith("http")]


async def _collect_client_routes(page: Page) -> list[str]:
    try:
        return await page.evaluate("() => window.__qauraRoutes || []")
    except Exception:
        return []


async def _record_response(result: ReconResult, response, url: str) -> int | None:
    if response is None:
        return None
    try:
        status = response.status
        result.statuses[url] = status
        if status == 429:
            result.saw_429 = True
        headers = await response.all_headers()
        if any(h.lower().startswith(("ratelimit", "x-ratelimit", "retry-after")) for h in headers):
            result.rate_limit_headers = True
        return status
    except Exception:
        return None


def _mark_volatile(primary: list[NumericElement], second: list[NumericElement]) -> None:
    """Any value that changed between two loads of the same URL is a clock, a counter or
    a rotating slot. An invariant referencing one would fail on a schedule, so it is
    excluded from the inventory entirely rather than merely down-weighted."""
    by_selector = {(e.container_selector, e.selector): e.number for e in second}
    for element in primary:
        key = (element.container_selector, element.selector)
        if key not in by_selector:
            element.volatile = True
        elif abs(by_selector[key] - element.number) > 1e-9:
            element.volatile = True


def _mark_viewport_fragile(primary: list[NumericElement], other: list[NumericElement]) -> None:
    present = {(e.container_selector, e.selector) for e in other}
    for element in primary:
        if (element.container_selector, element.selector) not in present:
            element.viewport_fragile = True


async def run_recon(
    seed_url: str,
    cfg: GuardrailConfig,
    limits: ReconLimits,
    *,
    storage_state: str | None = None,
    role: str | None = None,
    headless: bool = True,
    console: Console | None = None,
    login_form: bool = False,
    auth_dir: Path | None = None,
) -> ReconResult:
    result = ReconResult(seed_url=seed_url, final_url=seed_url, role=role,
                         max_depth=limits.max_depth)
    pacer = Pacer(cfg.max_requests_per_second, cfg.max_wall_clock_seconds)
    seed_parts = urlsplit(seed_url)
    origin = f"{seed_parts.scheme}://{seed_parts.netloc}"

    sitemap_urls, disallowed = await fetch_route_hints(
        seed_url, cfg, respect_robots=limits.respect_robots
    )
    result.robots_disallowed = disallowed
    if sitemap_urls:
        result.route_sources["sitemap"] = len(sitemap_urls)

    allow_once = AllowOnce(origin=origin)

    async with Driver(headless=headless) as driver:
        spec = ContextSpec(persona="init-recon", role=role, storage_state_path=storage_state,
                           viewport=limits.viewports[0])
        async with driver.context(spec) as (context, page):
            await install_readonly_routes(context, cfg, result.ledger, allow_once)
            if limits.interact_safe:
                await context.add_init_script(_HISTORY_HOOK_JS)
            page.set_default_navigation_timeout(limits.nav_timeout_ms)

            queue: deque[tuple[str, int]] = deque([(seed_url, 0)])
            for url in sitemap_urls:
                queue.append((url, 1))
            seen: set[str] = set()
            first = True

            while queue:
                if len(result.states) >= limits.max_pages:
                    result.truncated_reason = f"page cap ({limits.max_pages}) reached"
                    break
                if pacer.expired:
                    result.truncated_reason = f"time budget ({cfg.max_wall_clock_seconds}s) reached"
                    break

                url, depth = queue.popleft()
                template = url_template(url)
                if template in seen:
                    continue
                seen.add(template)

                await pacer.pace()
                result.attempted += 1
                started = asyncio.get_event_loop().time()
                try:
                    response = await guard_goto(page, url, cfg)
                except GuardrailViolation:
                    continue
                except Exception:
                    _log.debug("navigation failed for %s", url, exc_info=True)
                    continue
                result.nav_latencies_ms.append(
                    (asyncio.get_event_loop().time() - started) * 1000
                )
                await _settle(page, limits)

                challenge = await detect_challenge_page(page, response)
                if challenge.detected:
                    raise ChallengeDetected(challenge)

                status = await _record_response(result, response, url)
                if first:
                    result.final_url = page.url
                    first = False

                landed = urlsplit(page.url)
                if landed.hostname and landed.hostname != seed_parts.hostname:
                    result.subdomains_visited.add(landed.hostname)
                result.discovered_paths.append(landed.path or "/")

                model = await build_page_model(page)
                is_new = result.graph.is_new_state(model)
                result.graph.visit(model)
                if not is_new:
                    continue

                numerics = await scan_numeric_elements(page)
                html = await page.content()
                login_signal = await detect_login_wall(page, model, status)

                state = ObservedState(
                    key=fingerprint(model).key(),
                    url=page.url,
                    template=url_template(page.url),
                    title=await page.title(),
                    status=status,
                    depth=depth,
                    model=model,
                    numerics=numerics,
                    login_signal=login_signal,
                    html=html,
                )
                result.states.append(state)
                result.snapshots.append(
                    Snapshot(url=page.url, html=html, viewport=limits.viewports[0], load_index=0)
                )

                # Assisted login, once, as soon as a wall is actually seen.
                if login_form and login_signal.kind == "login_page" and not result.assisted_login:
                    outcome = await attempt_login(
                        page, context, allow_once, role=role or "user",
                        auth_dir=auth_dir or DEFAULT_AUTH_DIR,
                    )
                    result.assisted_login = outcome.reason
                    if console:
                        colour = "green" if outcome.ok else "yellow"
                        console.print(f"[{colour}]assisted login: {outcome.reason}[/{colour}]")
                    if outcome.ok:
                        # The session is live in this context now, so restart the crawl
                        # from the seed: everything gated is suddenly reachable.
                        queue.appendleft((seed_url, 0))
                        seen.discard(url_template(seed_url))
                        continue

                # A second load of the same URL is what identifies volatile values.
                if limits.loads_per_state > 1 and not pacer.expired:
                    await pacer.pace()
                    try:
                        await guard_goto(page, page.url, cfg)
                        await _settle(page, limits)
                        _mark_volatile(numerics, await scan_numeric_elements(page))
                        result.snapshots.append(Snapshot(
                            url=page.url, html=await page.content(),
                            viewport=limits.viewports[0], load_index=1,
                        ))
                    except Exception:
                        _log.debug("volatility re-load failed for %s", url, exc_info=True)

                # Responsive layouts hide and show content, so a value that only exists
                # at one width makes a weaker invariant than one that always renders.
                for viewport in limits.viewports[1:]:
                    if pacer.expired:
                        break
                    try:
                        await page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
                        await asyncio.sleep(0.2)
                        _mark_viewport_fragile(numerics, await scan_numeric_elements(page))
                        result.snapshots.append(Snapshot(
                            url=page.url, html=await page.content(),
                            viewport=viewport, load_index=0,
                        ))
                    except Exception:
                        _log.debug("viewport variation failed for %s", url, exc_info=True)
                try:
                    await page.set_viewport_size(
                        {"width": limits.viewports[0][0], "height": limits.viewports[0][1]}
                    )
                except Exception:
                    pass

                if limits.interact_safe:
                    await _interact_safe(page, cfg, limits, result)

                if depth >= limits.max_depth:
                    continue

                links = await _collect_links(page)
                links += await _collect_client_routes(page)
                queued = 0
                for link in links:
                    link_parts = urlsplit(link)
                    if link_parts.hostname and link_parts.hostname != seed_parts.hostname:
                        result.external_hosts.add(link_parts.hostname)
                    if not is_in_scope(link, cfg):
                        continue
                    if limits.respect_robots and robots_blocks(link_parts.path or "/", disallowed):
                        continue
                    if url_template(link) in seen:
                        continue
                    queue.append((link, depth + 1))
                    queued += 1
                if queued:
                    result.route_sources["links"] = result.route_sources.get("links", 0) + queued

    result.login = summarize_wall(result.states, result.attempted)
    return result


async def _interact_safe(page: Page, cfg: GuardrailConfig, limits: ReconLimits,
                         result: ReconResult) -> None:
    """Clicks display-only controls — tabs and disclosure toggles — to reveal content
    that is in the DOM but hidden. Every candidate is checked against is_destructive
    first, and any non-GET request the click provokes is aborted by the route handler
    regardless, so the worst case is a wasted click."""
    try:
        model = await build_page_model(page)
    except Exception:
        return

    start_url = page.url
    clicks = 0
    for element in model.elements:
        if clicks >= limits.max_interactions:
            return
        if element.role not in ("tab", "treeitem") or not element.visible:
            continue
        if is_destructive(element, cfg):
            continue
        try:
            locator = page.get_by_role(element.role, name=element.name, exact=True).nth(element.index)
            await locator.click(timeout=2000)
            clicks += 1
            await asyncio.sleep(0.15)
            if page.url != start_url:
                await guard_goto(page, start_url, cfg)
                await _settle(page, limits)
        except Exception:
            continue
    if clicks:
        result.route_sources["interact"] = result.route_sources.get("interact", 0) + clicks


async def probe_anon_differential(
    paths: list[str],
    seed_url: str,
    cfg: GuardrailConfig,
    limits: ReconLimits,
    *,
    headless: bool = True,
) -> dict[str, str]:
    """Which paths are actually privileged, measured rather than guessed.

    Visits each discovered path in a fresh anonymous context. A path that rendered fine
    under the authenticated session but 401s, 403s or bounces to a login page here is
    genuinely access-controlled — which is exactly what admin_paths is supposed to
    contain, and is not something a name-pattern guess can establish.
    """
    parts = urlsplit(seed_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    verdicts: dict[str, str] = {}
    pacer = Pacer(cfg.max_requests_per_second, cfg.max_wall_clock_seconds)
    ledger = ReadOnlyLedger()

    async with Driver(headless=headless) as driver:
        spec = ContextSpec(persona="init-anon", role=None, storage_state_path=None,
                           viewport=limits.viewports[0])
        async with driver.context(spec) as (context, page):
            await install_readonly_routes(context, cfg, ledger)
            page.set_default_navigation_timeout(limits.nav_timeout_ms)

            for path in paths:
                if pacer.expired:
                    break
                url = urljoin(origin, path)
                if not is_in_scope(url, cfg):
                    continue
                await pacer.pace()
                try:
                    response = await guard_goto(page, url, cfg)
                    await _settle(page, limits)
                except Exception:
                    continue

                status = response.status if response is not None else None
                if status in (401, 403):
                    verdicts[path] = f"HTTP {status} anonymously"
                    continue
                try:
                    model = await build_page_model(page)
                    signal = await detect_login_wall(page, model, status)
                except Exception:
                    continue
                if signal.kind == "login_page":
                    verdicts[path] = "redirected to a login page anonymously"
    return verdicts

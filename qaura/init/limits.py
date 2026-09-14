"""The guardrails recon runs under, built in code rather than read from config.

There is a bootstrap problem here: guardrails are what keep a crawl in bounds, but the
whole point of `qaura init` is to write the guardrails, so recon cannot read the config
it is producing. It therefore runs under a fixed, deliberately tight policy derived from
nothing but the seed URL.

Crawl shape (page cap, depth, viewports) lives in ReconLimits rather than in
GuardrailConfig on purpose. GuardrailConfig is a user-facing schema that every run
validates against; growing it with fields only this one command's bootstrap reads would
put internal knobs in front of every user forever.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from qaura.config import GuardrailConfig

# Never crawled, regardless of what the site links to. Broader than the eventual
# generated config, because recon has no idea yet which of these are real.
RECON_BLOCKED_PATHS = [
    "/logout*", "/log-out*", "/signout*", "/sign-out*", "/logoff*",
    "*/logout*", "*/signout*", "*/sign-out*",
    "*/delete*", "*/destroy*", "*/remove*", "*/purge*",
    "/checkout*", "*/checkout*", "/pay*", "*/payment*", "/billing*",
    "*/unsubscribe*", "*/deactivate*", "*/terminate*",
    # Downloads: nothing to observe, and a large file burns the wall-clock budget.
    "*.csv", "*.zip", "*.pdf", "*.xlsx", "*.xls", "*.doc*", "*.tar*", "*.gz", "*.dmg", "*.exe",
]


def recon_guardrails(seed_url: str, *, remote: bool = False) -> GuardrailConfig:
    """The policy recon itself obeys. `allow_destructive` is False and no flag can
    change it — there is no code path in `qaura init` that sets it True."""
    host = urlsplit(seed_url).hostname or "localhost"
    return GuardrailConfig(
        allowed_domains=[host],
        allowed_paths=["/**"],
        blocked_paths=list(RECON_BLOCKED_PATHS),
        allow_destructive=False,
        max_actions_per_run=0,
        max_requests_per_second=1.0 if remote else 2.0,
        max_wall_clock_seconds=180,
        max_llm_calls_per_run=6,
    )


@dataclass(frozen=True)
class ReconLimits:
    max_pages: int = 25
    max_depth: int = 2
    # Two loads of the same URL is what exposes volatile numbers — a clock, an "N users
    # online" counter, a rotating ad slot. A candidate invariant built on one of those
    # is a false positive on a timer, and one extra GET per state is a cheap way to
    # never write one.
    loads_per_state: int = 2
    viewports: tuple[tuple[int, int], ...] = ((1280, 800), (390, 844))
    nav_timeout_ms: int = 15_000
    settle_timeout_ms: int = 4_000
    interact_safe: bool = False
    max_llm_calls: int = 6
    max_interactions: int = 40
    respect_robots: bool = True

    def with_overrides(self, *, max_pages: int | None = None, max_depth: int | None = None,
                       interact_safe: bool | None = None,
                       respect_robots: bool | None = None) -> "ReconLimits":
        return ReconLimits(
            max_pages=max_pages if max_pages is not None else self.max_pages,
            max_depth=max_depth if max_depth is not None else self.max_depth,
            loads_per_state=self.loads_per_state,
            viewports=self.viewports,
            nav_timeout_ms=self.nav_timeout_ms,
            settle_timeout_ms=self.settle_timeout_ms,
            interact_safe=interact_safe if interact_safe is not None else self.interact_safe,
            max_llm_calls=self.max_llm_calls,
            max_interactions=self.max_interactions,
            respect_robots=respect_robots if respect_robots is not None else self.respect_robots,
        )


class Pacer:
    """Rate limiting for an async crawl.

    Not RunLimiter.throttle(): that one calls time.sleep(), which blocks the event loop
    and would stall every other coroutine on the same loop. Same intent, awaitable
    implementation.
    """

    def __init__(self, requests_per_second: float, wall_clock_seconds: int) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._wall_clock = wall_clock_seconds
        self._start = time.monotonic()
        self._last: float | None = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    @property
    def expired(self) -> bool:
        return self.elapsed > self._wall_clock

    async def pace(self) -> None:
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        if self._last is not None:
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last = time.monotonic()

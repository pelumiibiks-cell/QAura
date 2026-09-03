"""Evidence capture: console messages, network requests/failures, screenshots, and a
Playwright trace. This is what makes findings reproducible and debuggable — a Finding
(reporting/models.py, Phase 2) embeds what this module collected, not just a text
description of what allegedly went wrong.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import BrowserContext, ConsoleMessage, Page, Request, Response

_log = logging.getLogger(__name__)


@dataclass
class ConsoleEntry:
    type: str          # "log" | "error" | "warning" | ...
    text: str
    location: str | None
    timestamp: float


@dataclass
class NetworkEntry:
    url: str
    method: str
    status: int | None      # None if the request never got a response (aborted/failed)
    ok: bool | None
    failure_text: str | None
    resource_type: str
    timestamp: float


@dataclass
class PageCrash:
    message: str
    timestamp: float


class Recorder:
    """Attaches listeners to a Page (and its BrowserContext, for the trace) and buffers
    what it sees. One Recorder per persona/role session — call `start()` right after the
    page is created, `stop_and_save()` once per finding (or at session end) to flush a
    trace.zip and a screenshot alongside the buffered console/network logs.
    """

    def __init__(self, page: Page, context: BrowserContext) -> None:
        self._page = page
        self._context = context
        self.console: list[ConsoleEntry] = []
        self.network: list[NetworkEntry] = []
        self.crashes: list[PageCrash] = []
        self._tracing_started = False

    async def start(self, trace: bool = True) -> None:
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_page_error)
        self._page.on("requestfinished", self._on_request_finished)
        self._page.on("requestfailed", self._on_request_failed)
        if trace:
            await self._context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self._tracing_started = True

    def _on_console(self, msg: ConsoleMessage) -> None:
        loc = msg.location
        loc_str = f"{loc.get('url', '')}:{loc.get('lineNumber', '')}" if loc else None
        self.console.append(ConsoleEntry(
            type=msg.type, text=msg.text, location=loc_str, timestamp=time.time(),
        ))

    def _on_page_error(self, error: Exception) -> None:
        self.crashes.append(PageCrash(message=str(error), timestamp=time.time()))

    async def _on_request_finished(self, request: Request) -> None:
        try:
            response: Response | None = await request.response()
            status = response.status if response else None
            ok = response.ok if response else None
        except Exception:
            # ok=None here is indistinguishable downstream from "no response yet" —
            # detectors/network.py only flags ok is False, so a failure to read the
            # response is silently dropped rather than surfaced as a network finding.
            # Not changed here (that's a detection-semantics decision, not a logging
            # one), but at least visible now with --verbose.
            _log.debug("failed to read response for %s %s", request.method, request.url, exc_info=True)
            status, ok = None, None
        self.network.append(NetworkEntry(
            url=request.url, method=request.method, status=status, ok=ok,
            failure_text=None, resource_type=request.resource_type, timestamp=time.time(),
        ))

    def _on_request_failed(self, request: Request) -> None:
        self.network.append(NetworkEntry(
            url=request.url, method=request.method, status=None, ok=False,
            failure_text=request.failure, resource_type=request.resource_type,
            timestamp=time.time(),
        ))

    @property
    def console_errors(self) -> list[ConsoleEntry]:
        return [c for c in self.console if c.type == "error"]

    @property
    def network_failures(self) -> list[NetworkEntry]:
        return [n for n in self.network if n.ok is False]

    async def screenshot(self, out_path: str | Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(out_path), full_page=True)
        return out_path

    async def stop_and_save_trace(self, out_path: str | Path) -> Path | None:
        if not self._tracing_started:
            return None
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.tracing.stop(path=str(out_path))
        self._tracing_started = False
        return out_path

    def clear(self) -> None:
        """Call between independent action sequences if you want per-step evidence
        windows rather than one giant buffer for the whole session — the orchestrator
        (Phase 2/3) decides the granularity, this just provides the mechanism."""
        self.console.clear()
        self.network.clear()
        self.crashes.clear()

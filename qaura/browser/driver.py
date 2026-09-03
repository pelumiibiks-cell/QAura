"""Playwright lifecycle management. One BrowserContext per (persona, role) pair — this
is what lets the malicious persona hold an admin session and a user session
simultaneously for cross-role probing (plan's "Multi-role sessions" decision), and lets
personas run concurrently without their navigation state colliding.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


@dataclass
class ContextSpec:
    """Identifies one browser context. `role` maps to a saved storage-state file via
    config.AuthRole (e.g. "user", "admin") or None for an anonymous/unauthenticated
    context. `persona` is a label for logging/artifact naming, not behavior here —
    persona behavior lives in qaura/personas/, this module just gives it a browser."""

    persona: str
    role: str | None = None
    storage_state_path: str | None = None
    viewport: tuple[int, int] = (1280, 800)


class Driver:
    """Wraps one Playwright + Browser instance and hands out contexts per ContextSpec.
    Use as an async context manager so the browser is always closed even on error:

        async with Driver() as driver:
            async with driver.context(spec) as (context, page):
                ...
    """

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._playwright = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "Driver":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    @asynccontextmanager
    async def context(self, spec: ContextSpec) -> AsyncIterator[tuple[BrowserContext, Page]]:
        if self._browser is None:
            raise RuntimeError("Driver.context() used outside `async with Driver() as driver:`")

        kwargs: dict = {"viewport": {"width": spec.viewport[0], "height": spec.viewport[1]}}
        if spec.storage_state_path and Path(spec.storage_state_path).exists():
            kwargs["storage_state"] = spec.storage_state_path

        context = await self._browser.new_context(**kwargs)
        try:
            page = await context.new_page()
            try:
                yield context, page
            finally:
                await page.close()
        finally:
            await context.close()

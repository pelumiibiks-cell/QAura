"""Driver lifecycle without a real browser: fakes stand in for Playwright's start/launch/close."""
import pytest

import qaura.browser.driver as driver_module
from qaura.browser.driver import ContextSpec, Driver


class _FakePlaywright:
    def __init__(self, chromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _Starter:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self._playwright


def _install(monkeypatch, chromium) -> _FakePlaywright:
    playwright = _FakePlaywright(chromium)
    monkeypatch.setattr(driver_module, "async_playwright", lambda: _Starter(playwright))
    return playwright


async def test_playwright_is_stopped_when_the_browser_fails_to_launch(monkeypatch):
    class _Chromium:
        async def launch(self, headless=True):
            raise RuntimeError("no browser installed")

    playwright = _install(monkeypatch, _Chromium())
    with pytest.raises(RuntimeError, match="no browser installed"):
        async with Driver():
            pass
    assert playwright.stopped


async def test_playwright_is_stopped_even_if_closing_the_browser_fails(monkeypatch):
    class _Browser:
        async def close(self):
            raise RuntimeError("close failed")

    class _Chromium:
        async def launch(self, headless=True):
            return _Browser()

    playwright = _install(monkeypatch, _Chromium())
    with pytest.raises(RuntimeError, match="close failed"):
        async with Driver():
            pass
    assert playwright.stopped


async def test_read_only_contexts_block_service_workers(monkeypatch):
    captured: list[dict] = []

    class _Page:
        async def close(self):
            pass

    class _Context:
        async def new_page(self):
            return _Page()

        async def close(self):
            pass

    class _Browser:
        async def new_context(self, **kwargs):
            captured.append(kwargs)
            return _Context()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, headless=True):
            return _Browser()

    _install(monkeypatch, _Chromium())
    async with Driver() as driver:
        async with driver.context(ContextSpec(persona="recon", block_service_workers=True)):
            pass
        async with driver.context(ContextSpec(persona="run")):
            pass
    assert captured[0]["service_workers"] == "block"
    assert "service_workers" not in captured[1]

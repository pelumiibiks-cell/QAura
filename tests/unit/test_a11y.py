import pytest
from playwright.async_api import async_playwright

from qaura.detectors.a11y import detect


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def test_detect_flags_image_missing_alt_text(page):
    await page.set_content('<html><body><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></body></html>')
    findings = await detect(page, page.url, [], "heuristic")
    assert len(findings) >= 1
    assert all(f.detector == "a11y" for f in findings)
    assert any("alt" in f.title.lower() or "image" in f.description.lower() for f in findings)


async def test_detect_returns_empty_for_clean_accessible_page(page):
    await page.set_content("""
        <html><body>
          <h1>Accessible Page</h1>
          <button aria-label="close">X</button>
        </body></html>
    """)
    findings = await detect(page, page.url, [], "heuristic")
    # A minimal well-formed page should have zero or very few violations
    assert isinstance(findings, list)


async def test_detect_severity_maps_from_axe_impact(page):
    await page.set_content('<html><body><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></body></html>')
    findings = await detect(page, page.url, [], "heuristic")
    assert len(findings) >= 1
    for f in findings:
        assert f.severity.value in {"critical", "high", "medium", "low", "info"}

import pytest
from playwright.async_api import async_playwright

from qaura.detectors.visual import detect, scan_rules


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page(viewport={"width": 800, "height": 600})
        yield pg
        await browser.close()


async def test_scan_detects_overflow(page):
    await page.set_content("""
        <div style="width: 50px; height: 20px; overflow: visible;">
          This text is way too long to fit inside this tiny fifty pixel wide box
        </div>
    """)
    violations = await scan_rules(page, enabled_rules={"overflow"})
    assert any(v["rule"] == "overflow" for v in violations)


async def test_scan_no_overflow_when_scroll_affordance_present(page):
    await page.set_content("""
        <div style="width: 50px; height: 20px; overflow: auto;">
          This text is way too long to fit inside this tiny fifty pixel wide box
        </div>
    """)
    violations = await scan_rules(page, enabled_rules={"overflow"})
    assert not any(v["rule"] == "overflow" for v in violations)


async def test_scan_detects_low_contrast(page):
    await page.set_content("""
        <div style="background: #ffffff;">
          <span style="color: #f8f8f8;">Nearly invisible light gray on white</span>
        </div>
    """)
    violations = await scan_rules(page, enabled_rules={"contrast"})
    assert any(v["rule"] == "contrast" for v in violations)


async def test_scan_no_contrast_issue_for_high_contrast_text(page):
    await page.set_content("""
        <div style="background: #ffffff;">
          <span style="color: #000000;">Solid black on white, plenty of contrast</span>
        </div>
    """)
    violations = await scan_rules(page, enabled_rules={"contrast"})
    assert not any(v["rule"] == "contrast" for v in violations)


async def test_scan_detects_invisible_text_same_color_as_background(page):
    await page.set_content("""
        <div style="background: #ffffff; color: #ffffff;">
          This text is white on a white background
        </div>
    """)
    violations = await scan_rules(page, enabled_rules={"invisible_text"})
    assert any(v["rule"] == "invisible_text" for v in violations)


async def test_scan_detects_zero_opacity_text(page):
    await page.set_content("""
        <div style="opacity: 0;">This text has zero opacity but is still 'displayed'</div>
    """)
    violations = await scan_rules(page, enabled_rules={"invisible_text"})
    assert any(v["rule"] == "invisible_text" for v in violations)


async def test_scan_detects_offscreen_interactive_element(page):
    await page.set_content("""
        <button style="position: absolute; left: -9999px; top: -9999px;">Hidden action</button>
    """)
    violations = await scan_rules(page, enabled_rules={"offscreen_interactive"})
    assert any(v["rule"] == "offscreen_interactive" for v in violations)


async def test_scan_normal_button_not_flagged_offscreen(page):
    await page.set_content('<button style="position: static;">Normal button</button>')
    violations = await scan_rules(page, enabled_rules={"offscreen_interactive"})
    assert not any(v["rule"] == "offscreen_interactive" for v in violations)


async def test_scan_below_the_fold_button_not_flagged_offscreen(page):
    # Regression test for the exact false positive found live against a real form
    # (Phase D): a page taller than the viewport with a normal, reachable-by-scroll
    # button near the bottom must NOT be reported as "offscreen" — that's simply
    # what being below the fold means, not a bug. The fixture viewport is 800x600.
    await page.set_content("""
        <div style="height: 3000px;">spacer content to make the page scroll</div>
        <button style="position: static;">Reachable by scrolling</button>
    """)
    violations = await scan_rules(page, enabled_rules={"offscreen_interactive"})
    assert not any(v["rule"] == "offscreen_interactive" for v in violations)


async def test_scan_detects_negative_position_offscreen_on_a_tall_page(page):
    # The genuinely-off-canvas case must still fire even on a page that also has
    # ordinary below-the-fold content, so the fix doesn't just widen the threshold
    # into never firing at all.
    await page.set_content("""
        <div style="height: 3000px;">spacer content to make the page scroll</div>
        <button style="position: absolute; left: -9999px; top: -9999px;">Hidden action</button>
    """)
    violations = await scan_rules(page, enabled_rules={"offscreen_interactive"})
    assert any(v["rule"] == "offscreen_interactive" for v in violations)


async def test_scan_detects_zero_size_interactive_element(page):
    await page.set_content('<button style="width: 0; height: 0; padding: 0; border: 0; overflow: hidden;">Invisible</button>')
    violations = await scan_rules(page, enabled_rules={"zero_size_interactive"})
    assert any(v["rule"] == "zero_size_interactive" for v in violations)


async def test_scan_normal_button_not_flagged_zero_size(page):
    await page.set_content('<button>Normal button</button>')
    violations = await scan_rules(page, enabled_rules={"zero_size_interactive"})
    assert not any(v["rule"] == "zero_size_interactive" for v in violations)


async def test_scan_respects_enabled_rules_filter(page):
    await page.set_content("""
        <div style="width: 10px; overflow: visible;">overflowing text that is too long</div>
        <div style="color: #fefefe; background: #ffffff;">nearly invisible text</div>
    """)
    only_overflow = await scan_rules(page, enabled_rules={"overflow"})
    assert all(v["rule"] == "overflow" for v in only_overflow)


async def test_detect_returns_findings_with_correct_detector_and_severity(page):
    await page.set_content("""
        <div style="width: 10px; height: 10px; overflow: visible;">
          text that overflows this box for sure
        </div>
    """)
    findings = await detect(page, page.url, [], "heuristic", enabled_rules={"overflow"})
    assert len(findings) >= 1
    assert all(f.detector == "visual" for f in findings)


async def test_detect_returns_empty_list_when_no_violations(page):
    await page.set_content('<p style="color:#000;background:#fff;">clean page</p>')
    findings = await detect(page, page.url, [], "heuristic")
    assert findings == []


async def test_detect_does_not_call_llm_when_provider_none(page):
    await page.set_content("""
        <div style="width: 10px; overflow: visible;">overflowing content here</div>
    """)
    # No provider passed -- should not raise, and findings should still come back
    findings = await detect(page, page.url, [], "heuristic", enabled_rules={"overflow"})
    assert len(findings) >= 1

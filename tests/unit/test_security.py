import pytest
from playwright.async_api import async_playwright

from qaura.detectors.security import check_cross_role_access, detect_reflected_injection


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        yield pg
        await browser.close()


# --- detect_reflected_injection ---------------------------------------------------

async def test_detects_unescaped_html_tag_injection(page):
    # Simulates a vulnerable page that inserted user input via innerHTML instead of
    # textContent -- the marker tag becomes a real (if unknown) DOM element.
    await page.set_content('<html><body><div id="out"></div></body></html>')
    await page.evaluate("document.getElementById('out').innerHTML = '<qaura-marker>test</qaura-marker>'")
    findings = await detect_reflected_injection(page, page.url, [], "malicious")
    assert len(findings) == 1
    assert findings[0].detector == "security"
    assert findings[0].severity.value == "high"
    assert "Unescaped HTML" in findings[0].title


async def test_detects_executed_script_injection(page):
    await page.set_content('<html><body><div id="out"></div></body></html>')
    await page.evaluate("document.getElementById('out').innerHTML = '<img src=x onerror=\"window.__qaura_marker=1\">'")
    findings = await detect_reflected_injection(page, page.url, [], "malicious")
    assert len(findings) == 1
    assert findings[0].severity.value == "critical"
    assert "executed" in findings[0].title.lower()


async def test_no_finding_when_input_properly_escaped(page):
    # Simulates a SAFE page: user input inserted via textContent, so the marker is
    # displayed as literal text, never parsed as an element.
    await page.set_content('<html><body><div id="out"></div></body></html>')
    await page.evaluate("document.getElementById('out').textContent = '<qaura-marker>test</qaura-marker>'")
    findings = await detect_reflected_injection(page, page.url, [], "malicious")
    assert findings == []


async def test_no_finding_on_clean_page(page):
    await page.set_content("<html><body><p>Nothing suspicious here</p></body></html>")
    findings = await detect_reflected_injection(page, page.url, [], "malicious")
    assert findings == []


# --- check_cross_role_access -------------------------------------------------------

async def test_flags_reachable_page_with_no_block_signal(page):
    await page.set_content("""
        <html><body>
          <h1>Admin Settings</h1>
          <button>Delete all users</button>
        </body></html>
    """)
    finding = await check_cross_role_access(page, page.url, "user", [], "malicious")
    assert finding is not None
    assert finding.detector == "security"
    assert "user" in finding.description


async def test_no_finding_when_login_form_present(page):
    await page.set_content("""
        <html><body>
          <form>
            <label>Password <input type="password" name="password"></label>
            <button type="submit">Log in</button>
          </form>
        </body></html>
    """)
    finding = await check_cross_role_access(page, page.url, "user", [], "malicious")
    assert finding is None


async def test_no_finding_when_forbidden_text_present(page):
    await page.set_content("<html><body><h1>403 Forbidden</h1><p>Access denied.</p></body></html>")
    finding = await check_cross_role_access(page, page.url, "user", [], "malicious")
    assert finding is None

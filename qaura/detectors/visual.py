"""Visual detector — plan design decision #7: deterministic geometry/CSS rules run
on every page for free (no LLM), a vision model only confirms when a rule actually
fires. The rule layer runs as one `page.evaluate()` call (a JS DOM walk) rather than
many Python round-trips per element — checking contrast/overflow/visibility for every
element individually via separate Playwright calls would be both slow and awkward,
since computed-style comparisons (walking up for an effective background color, WCAG
luminance math) are naturally DOM-side operations.

Five rules, each independently toggleable via `enabled_rules`:
  - overflow: content wider/taller than its container with no scroll affordance
  - contrast: WCAG-style contrast ratio below threshold for visible text
  - invisible_text: non-empty text that's effectively unreadable (zero font-size,
    fg == bg color, opacity 0) while the element otherwise reports as displayed
  - offscreen_interactive: an interactive element (button/link/input/...) positioned
    off-canvas — negative coordinates, or beyond the document's own rendered bounds.
    Phase D fix: this used to compare against the current VIEWPORT height, which
    means it flagged every element below the fold on any page taller than one
    screenful — that's what "below the fold" *is*, not a bug (the crawler now
    scrolls to reach them anyway, and Playwright's own action executors auto-scroll
    regardless). Genuinely off-canvas is a different thing: negative position (the
    classic `top:-9999px` visually-hidden trick applied to something that should be
    interactive) or positioned beyond the document's own scrollWidth/scrollHeight.
    Known limitation, not solved here: an absolutely-positioned element placed far
    outside normal content can itself expand `scrollHeight` to include its own
    position, which makes it invisible to a scrollHeight-bounds check too — no
    purely-geometric check catches every off-canvas trick. This fix removes a
    guaranteed false positive on any page with a form or a scroll; it does not
    claim to catch every possible off-canvas case.
  - zero_size_interactive: an interactive element with zero rendered width or
    height — a control that exists in the DOM and accessibility tree but literally
    cannot be seen or clicked.
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

from qaura.llm.base import ImagePart, LLMProvider, Tier
from qaura.reporting.models import Evidence, Finding, ReproStep, Severity

_log = logging.getLogger(__name__)

DEFAULT_ENABLED_RULES = {"overflow", "contrast", "invisible_text", "offscreen_interactive", "zero_size_interactive"}

_DOC_BOUNDS_JS = "() => ({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight})"

# The JS walk. Returns a flat list of {rule, selector_hint, detail} dicts. Kept as one
# script (not several) so the DOM is only walked once per rule-relevant node.
_SCAN_JS = r"""
() => {
  const results = [];
  const viewportW = window.innerWidth, viewportH = window.innerHeight;

  function luminance(r, g, b) {
    const c = [r, g, b].map(v => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  function parseRgb(str) {
    const m = str.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
  }
  function effectiveBackground(el) {
    let node = el;
    while (node) {
      const bg = parseRgb(getComputedStyle(node).backgroundColor);
      if (bg && bg.a > 0) return bg;
      node = node.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  }
  function describe(el) {
    const name = el.getAttribute('data-testid') || el.id || el.tagName.toLowerCase();
    const text = (el.textContent || '').trim().slice(0, 40);
    return text ? `${name} ("${text}")` : name;
  }

  const all = document.querySelectorAll('body *');
  for (const el of all) {
    const style = getComputedStyle(el);
    if (style.display === 'none') continue;
    const rect = el.getBoundingClientRect();

    // --- overflow ---
    const overflowsX = el.scrollWidth > el.clientWidth + 2;
    const overflowsY = el.scrollHeight > el.clientHeight + 2;
    const hasScrollAffordance = /(auto|scroll)/.test(style.overflow + style.overflowX + style.overflowY);
    if ((overflowsX || overflowsY) && !hasScrollAffordance && rect.width > 0) {
      results.push({ rule: 'overflow', detail: `${describe(el)}: content overflows its box with no scroll affordance` });
    }

    // --- invisible / effectively-unreadable text ---
    const hasOwnText = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (hasOwnText && style.visibility !== 'hidden') {
      const fontSize = parseFloat(style.fontSize);
      const fg = parseRgb(style.color);
      const bg = effectiveBackground(el);
      const sameColor = fg && bg && fg.r === bg.r && fg.g === bg.g && fg.b === bg.b;
      const zeroOpacity = parseFloat(style.opacity) === 0;
      if (fontSize === 0 || sameColor || zeroOpacity) {
        results.push({ rule: 'invisible_text', detail: `${describe(el)}: text present but effectively invisible (fontSize=${fontSize}, sameColor=${sameColor}, opacity=${style.opacity})` });
      } else if (fg && bg) {
        // --- contrast (only meaningful when not already flagged invisible) ---
        const l1 = luminance(fg.r, fg.g, fg.b), l2 = luminance(bg.r, bg.g, bg.b);
        const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        const isLarge = fontSize >= 18 || (fontSize >= 14 && (parseInt(style.fontWeight) >= 700 || style.fontWeight === 'bold'));
        const threshold = isLarge ? 3.0 : 4.5;
        if (ratio < threshold) {
          results.push({ rule: 'contrast', detail: `${describe(el)}: contrast ratio ${ratio.toFixed(2)} below ${threshold} threshold` });
        }
      }
    }
  }
  return results;
}
"""


async def scan_rules(page: Page, enabled_rules: set[str] = DEFAULT_ENABLED_RULES) -> list[dict]:
    """Runs the DOM-side rule scan plus the offscreen-interactive check (which needs
    the interactive-element list, computed in Python from build_page_model's caller —
    kept separate since it's cheap and doesn't need the big JS walk)."""
    # Same "called after every action, page might be mid-navigation" hazard as
    # detectors/security.py — unguarded, this used to propagate an "execution
    # context was destroyed" error all the way out of the crawl loop.
    try:
        raw = await page.evaluate(_SCAN_JS)
    except Exception:
        _log.debug("visual rule scan failed to evaluate on %s", page.url, exc_info=True)
        return []
    violations = [v for v in raw if v["rule"] in enabled_rules]

    if "offscreen_interactive" in enabled_rules or "zero_size_interactive" in enabled_rules:
        from qaura.browser.observe import build_page_model

        model = await build_page_model(page)
        try:
            doc_bounds = await page.evaluate(_DOC_BOUNDS_JS)
        except Exception:
            doc_bounds = None
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        doc_w = doc_bounds["w"] if doc_bounds else viewport["width"]
        doc_h = doc_bounds["h"] if doc_bounds else viewport["height"]

        for el in model.elements:
            if el.bbox is None:
                continue
            x, y, w, h = el.bbox
            # Zero-size deliberately checked BEFORE the `el.visible` gate below —
            # Playwright's own is_visible() returns False for a zero-size element by
            # definition (confirmed live: bounding_box() still returns {w:0, h:0}
            # while is_visible() is False), so gating on el.visible first would make
            # this rule unable to ever fire.
            if "zero_size_interactive" in enabled_rules and (w <= 0 or h <= 0):
                violations.append({
                    "rule": "zero_size_interactive",
                    "detail": f"{el.role} {el.name!r} ({el.ref}) has zero rendered width/height",
                })
                continue  # a zero-size element can't also be meaningfully "off-canvas"

            if "offscreen_interactive" not in enabled_rules or not el.visible:
                continue
            if x + w < 0 or y + h < 0:
                violations.append({
                    "rule": "offscreen_interactive",
                    "detail": (
                        f"{el.role} {el.name!r} ({el.ref}) is positioned off-canvas "
                        f"(negative coordinates: x={x:.0f}, y={y:.0f})"
                    ),
                })
            elif x > doc_w or y > doc_h:
                violations.append({
                    "rule": "offscreen_interactive",
                    "detail": (
                        f"{el.role} {el.name!r} ({el.ref}) is positioned beyond the document's "
                        f"rendered bounds (x={x:.0f}, y={y:.0f}; document is {doc_w:.0f}x{doc_h:.0f})"
                    ),
                })
    return violations


async def detect(
    page: Page,
    url: str,
    repro_steps: list[ReproStep],
    persona: str = "heuristic",
    enabled_rules: set[str] = DEFAULT_ENABLED_RULES,
    provider: LLMProvider | None = None,
    screenshot_path: str | None = None,
) -> list[Finding]:
    """Rule violations become findings directly (no LLM needed to report them — the
    rule already IS the confirmation). If a provider is given and available, ALSO
    sends one screenshot + the rule list to the vision-confirm tier for a second
    opinion in the finding's description — not required for the finding to exist,
    since a fired geometry/contrast rule is already a legitimate signal on its own;
    the vision pass just adds color, matching the plan's "rules first, vision model
    on suspicion" cost-bounding design rather than gating the finding on it."""
    violations = await scan_rules(page, enabled_rules)
    if not violations:
        return []

    findings: list[Finding] = []
    for v in violations:
        findings.append(Finding(
            title=f"Visual issue ({v['rule']}): {v['detail'][:80]}",
            detector="visual",
            severity=Severity.LOW if v["rule"] == "contrast" else Severity.MEDIUM,
            persona=persona,
            url=url,
            description=v["detail"],
            repro_steps=list(repro_steps),
            evidence=Evidence(screenshot_path=screenshot_path),
        ))

    if provider is not None and provider.available and screenshot_path:
        summary = "; ".join(v["detail"] for v in violations[:5])
        try:
            with open(screenshot_path, "rb") as f:
                image_bytes = f.read()
            response = provider.complete(
                tier=Tier.VISUAL_CONFIRM,
                system=(
                    "You are confirming automated visual-QA findings against a screenshot. "
                    "Be brief: say whether the screenshot visibly supports the reported issues."
                ),
                input=f"Reported issues: {summary}",
                images=[ImagePart(mime_type="image/png", data=image_bytes)],
            )
            if response.text:
                for f in findings:
                    f.description += f"\n\nVision model note: {response.text.strip()[:300]}"
        except Exception:
            # vision confirmation is best-effort — the rule-based findings stand on
            # their own even if this fails
            _log.debug("vision-confirm call failed", exc_info=True)

    return findings

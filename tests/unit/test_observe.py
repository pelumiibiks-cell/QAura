"""Unit tests for PageModel/ElementInfo logic that doesn't need a live browser —
signature hashing, prompt rendering, ref lookup — plus the aria_snapshot text parser,
using fixture strings captured from real `page.aria_snapshot()` calls (see comments).
The full build_page_model() against a real page is exercised manually via
`qaura observe` against a live URL; that's an integration concern, not a unit one.
"""
from qaura.browser.observe import ElementInfo, PageModel, parse_aria_snapshot


def _model(elements=None) -> PageModel:
    return PageModel(url="http://example.test/page", title="Test Page", elements=elements or [])


def test_signature_is_stable_across_element_reordering():
    a = _model([
        ElementInfo(ref="e1", role="button", name="Add to cart"),
        ElementInfo(ref="e2", role="link", name="Home"),
    ])
    b = _model([
        ElementInfo(ref="e9", role="link", name="Home"),      # different ref, same content
        ElementInfo(ref="e8", role="button", name="Add to cart"),
    ])
    assert a.signature() == b.signature()


def test_signature_differs_when_content_differs():
    a = _model([ElementInfo(ref="e1", role="button", name="Add to cart")])
    b = _model([ElementInfo(ref="e1", role="button", name="Remove from cart")])
    assert a.signature() != b.signature()


def test_signature_ignores_url_only_state_like_product_id():
    # This is the exact plan example: /product/1 and /product/2 with the same
    # interactive-element signature should fingerprint identically.
    a = PageModel(url="http://shop.test/product/1", title="Widget", elements=[
        ElementInfo(ref="e1", role="button", name="Add to cart"),
    ])
    b = PageModel(url="http://shop.test/product/2", title="Widget", elements=[
        ElementInfo(ref="e1", role="button", name="Add to cart"),
    ])
    assert a.signature() == b.signature()


def test_find_returns_none_for_missing_ref():
    model = _model([ElementInfo(ref="e1", role="button", name="Submit")])
    assert model.find("e404") is None
    assert model.find("e1") is not None


def test_to_prompt_line_includes_state_flags():
    el = ElementInfo(ref="e3", role="checkbox", name="Subscribe", checked=True, enabled=False)
    line = el.to_prompt_line()
    assert "e3" in line
    assert "checkbox" in line
    assert "Subscribe" in line
    assert "checked=True" in line
    assert "disabled" in line


def test_to_prompt_handles_empty_element_list():
    model = _model([])
    text = model.to_prompt()
    assert "(none found)" in text
    assert "Test Page" in text


# Captured live from `await page.aria_snapshot()` against example.com (2026-08-30,
# Playwright 1.62.0) — page.accessibility.snapshot() was tried first and confirmed
# removed in this version (AttributeError) before switching to this API.
EXAMPLE_COM_SNAPSHOT = (
    '- heading "Example Domain" [level=1]\n'
    '- paragraph: This domain is for use in documentation examples without needing '
    'permission. Avoid use in operations.\n'
    '- paragraph:\n'
    '  - link "Learn more":\n'
    '    - /url: https://iana.org/domains/example\n'
)

# Captured live from a local form fixture with a textbox (with value), a checked
# checkbox, an enabled and a disabled button, and a combobox with a selected option.
FORM_SNAPSHOT = (
    '- heading "Test Form" [level=1]\n'
    '- text: Email\n'
    '- textbox "Email": a@b.com\n'
    '- checkbox "Subscribe" [checked]\n'
    '- text: Subscribe\n'
    '- button "Submit"\n'
    '- button "Disabled Btn" [disabled]\n'
    '- combobox:\n'
    '  - option "One" [selected]\n'
    '  - option "Two"\n'
    '- link "A link":\n'
    '  - /url: /foo\n'
)


def test_parse_aria_snapshot_extracts_heading_and_link():
    nodes = parse_aria_snapshot(EXAMPLE_COM_SNAPSHOT)
    roles = [(n.role, n.name) for n in nodes]
    assert ("heading", "Example Domain") in roles
    assert ("link", "Learn more") in roles
    # the /url property line must not become a spurious node
    assert not any(n.role == "url" for n in nodes)


def test_parse_aria_snapshot_extracts_textbox_value():
    nodes = parse_aria_snapshot(FORM_SNAPSHOT)
    textbox = next(n for n in nodes if n.role == "textbox")
    assert textbox.name == "Email"
    assert textbox.value == "a@b.com"


def test_parse_aria_snapshot_extracts_checked_and_disabled_flags():
    nodes = parse_aria_snapshot(FORM_SNAPSHOT)
    checkbox = next(n for n in nodes if n.role == "checkbox")
    assert checkbox.attrs.get("checked") is True

    disabled_button = next(n for n in nodes if n.role == "button" and n.name == "Disabled Btn")
    assert disabled_button.attrs.get("disabled") is True

    enabled_button = next(n for n in nodes if n.role == "button" and n.name == "Submit")
    assert "disabled" not in enabled_button.attrs


def test_parse_aria_snapshot_walks_into_combobox_options():
    nodes = parse_aria_snapshot(FORM_SNAPSHOT)
    options = [n for n in nodes if n.role == "option"]
    assert {n.name for n in options} == {"One", "Two"}
    selected = next(n for n in options if n.name == "One")
    assert selected.attrs.get("selected") is True


def test_parse_aria_snapshot_handles_empty_string():
    assert parse_aria_snapshot("") == []


def test_build_page_model_end_to_end_from_form_snapshot():
    """Exercises the same role-filtering + checked-derivation logic build_page_model()
    uses, without needing a live Page — feeds the fixture text through the parser and
    the INTERACTIVE_ROLES/CHECKABLE_ROLES filters directly."""
    from qaura.browser.observe import CHECKABLE_ROLES, INTERACTIVE_ROLES

    nodes = parse_aria_snapshot(FORM_SNAPSHOT)
    interactive = [n for n in nodes if n.role in INTERACTIVE_ROLES]
    roles_seen = {n.role for n in interactive}
    # heading/text/combobox-container aren't in INTERACTIVE_ROLES by design (combobox
    # itself IS interactive, "text" and "heading" are not)
    assert "combobox" in roles_seen
    assert "text" not in roles_seen
    assert "heading" not in roles_seen

    checkbox = next(n for n in interactive if n.role == "checkbox")
    assert checkbox.role in CHECKABLE_ROLES

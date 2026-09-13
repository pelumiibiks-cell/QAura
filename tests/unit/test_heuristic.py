from playwright.async_api import async_playwright

from qaura.browser.actions import Action, ActionKind
from qaura.browser.observe import ElementInfo, PageModel
from qaura.browser.recorder import Recorder
from qaura.config import QAuraConfig
from qaura.core.heuristic import HeuristicCrawler, _actions_for
from qaura.reporting.models import Finding


def test_textbox_gets_fill_and_tab_sequence():
    el = ElementInfo(ref="e1", role="textbox", name="Email")
    actions = _actions_for(el)
    assert len(actions) > 0
    assert all(a.kind in (ActionKind.FILL, ActionKind.KEY) for a in actions)
    fills = [a for a in actions if a.kind == ActionKind.FILL]
    assert all(a.ref == "e1" for a in fills)


def test_spinbutton_gets_fill_sequence_not_click():
    # Real bug found while wiring the invariants engine: a quantity input (role
    # spinbutton, e.g. <input type=number>) was falling through to the generic
    # CLICK-only branch, meaning heuristic mode could never actually change a
    # quantity value — clicking a number input just focuses it. core/inputs.py
    # already had spinbutton support (values_for_role), heuristic.py's action
    # selection just never used it. This locks the fix in.
    el = ElementInfo(ref="e2", role="spinbutton", name="Quantity")
    actions = _actions_for(el)
    fill_actions = [a for a in actions if a.kind == ActionKind.FILL]
    assert len(fill_actions) > 0, "spinbutton should get FILL actions, not just CLICK"


def test_checkbox_gets_single_check_action():
    el = ElementInfo(ref="e3", role="checkbox", name="Subscribe")
    actions = _actions_for(el)
    assert len(actions) == 1
    assert actions[0].kind == ActionKind.CHECK


def test_button_gets_single_click_action():
    el = ElementInfo(ref="e4", role="button", name="Submit")
    actions = _actions_for(el)
    assert len(actions) == 1
    assert actions[0].kind == ActionKind.CLICK


def _search_form_model(url: str, result_count: int) -> PageModel:
    """A search form plus `result_count` result rows — models a GET-method search
    form whose own results change the element signature, which is exactly what
    caused the live bug below (varying `result_count` is the analogue of varying
    query text producing different result content each time)."""
    elements = [
        ElementInfo(ref="e1", role="textbox", name="Search", form_key="search-form"),
        ElementInfo(ref="e2", role="button", name="Search", form_key="search-form", input_type="submit"),
    ]
    for i in range(result_count):
        elements.append(ElementInfo(ref=f"r{i}", role="link", name=f"Result {i}"))
    return PageModel(url=url, title="Records", elements=elements)


def test_form_marked_exercised_is_not_reoffered_on_same_template():
    # Live bug (Phase B/C): _mark_form_exercised recorded the form's ELEMENTS as
    # exercised in the state graph but never actually added the form itself to the
    # `_form_exercised` tracking set — so _next_unexercised_form kept returning the
    # same form forever regardless of how it was keyed. Compounded by a GET search
    # form's results changing the state fingerprint on every submission, this made
    # the crawler loop on one search form until the action budget ran out, never
    # reaching anything past it. This test locks in both halves of the fix: the
    # form must actually be recorded, and lookup must be keyed by URL template (not
    # by the full, content-varying state) so a changed result count doesn't make an
    # already-exercised form look new again.
    crawler = HeuristicCrawler("http://shop.test/records", QAuraConfig())

    model1 = _search_form_model("http://shop.test/records", result_count=0)
    node1 = crawler.graph.visit(model1)
    form = crawler._next_unexercised_form(model1, "http://shop.test/records")
    assert form is not None and form.key == "search-form"

    crawler._mark_form_exercised("http://shop.test/records", node1.fingerprint.key(), form)

    # Same URL template, but the search actually ran and produced different results
    # — a different STATE (different element signature) but the same page template.
    model2 = _search_form_model("http://shop.test/records", result_count=3)
    crawler.graph.visit(model2)
    assert crawler._next_unexercised_form(model2, "http://shop.test/records") is None


# --- crash safety -------------------------------------------------------------

_TARGET = "http://crawl.test/"


async def _run_crawler(crawler: HeuristicCrawler):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            context = await browser.new_context()
            await context.route(
                "http://crawl.test/**",
                lambda route: route.fulfill(body="<button>Go</button>", content_type="text/html"),
            )
            page = await context.new_page()
            recorder = Recorder(page, context)
            await recorder.start(trace=False)
            return await crawler.run(page, recorder)
        finally:
            await browser.close()


async def test_crawl_ends_cleanly_when_the_page_cannot_be_observed(monkeypatch):
    import qaura.browser.observe as observe

    async def broken(page):
        raise RuntimeError("Execution context was destroyed")

    monkeypatch.setattr(observe, "build_page_model", broken)
    result = await _run_crawler(HeuristicCrawler(_TARGET, QAuraConfig()))
    assert result.aborted_reason is not None
    assert "could not be observed" in result.aborted_reason


async def test_unexpected_error_mid_crawl_keeps_earlier_findings(monkeypatch):
    async def explode(self, page, url, repro_steps, persona):
        self.findings.append(Finding(title="found before the crash", detector="crash", url=url))
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(HeuristicCrawler, "_check_per_state", explode)
    result = await _run_crawler(HeuristicCrawler(_TARGET, QAuraConfig()))
    assert "found before the crash" in [f.title for f in result.findings]
    assert "RuntimeError" in result.aborted_reason


async def test_action_that_raises_still_counts_against_the_action_cap(monkeypatch):
    import qaura.core.heuristic as heuristic

    async def raising_execute(page, model, action):
        raise RuntimeError("timed out")

    class _Page:
        url = "http://shop.test/"

    monkeypatch.setattr(heuristic, "execute", raising_execute)
    crawler = HeuristicCrawler("http://shop.test/", QAuraConfig())
    element = ElementInfo(ref="e1", role="button", name="Go")
    model = PageModel(url="http://shop.test/", title="t", elements=[element])

    outcome = await crawler._do_action(
        _Page(), model, Action(kind=ActionKind.CLICK, ref="e1"), element,
        recorder=None, persona="heuristic", state_key="s",
    )
    assert outcome == "ok"
    assert crawler.limiter.actions_taken == 1
    assert crawler.findings[0].detector == "crash"

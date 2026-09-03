from qaura.browser.observe import ElementInfo, PageModel
from qaura.core.state import StateGraph, normalize_path, url_template


def test_normalize_path_replaces_numeric_segment():
    assert normalize_path("/product/123") == "/product/{id}"


def test_normalize_path_replaces_uuid_segment():
    assert normalize_path("/orders/550e8400-e29b-41d4-a716-446655440000") == "/orders/{id}"


def test_normalize_path_replaces_hex_hash_segment():
    assert normalize_path("/files/deadbeefcafef00d") == "/files/{id}"


def test_normalize_path_keeps_meaningful_segments():
    assert normalize_path("/admin/settings") == "/admin/settings"
    assert normalize_path("/checkout") == "/checkout"


def test_normalize_path_mixed_alnum_id():
    assert normalize_path("/invite/ab12cd34ef56") == "/invite/{id}"


def test_url_template_drops_query_and_fragment():
    a = url_template("http://shop.test/product/1?ref=email#reviews")
    b = url_template("http://shop.test/product/1")
    assert a == b == "http://shop.test/product/{id}"


def _model(url: str, names: list[str]) -> PageModel:
    return PageModel(
        url=url, title="t",
        elements=[ElementInfo(ref=f"e{i}", role="button", name=n) for i, n in enumerate(names)],
    )


def test_product_1_and_product_2_collapse_to_same_state():
    # the exact plan example
    graph = StateGraph()
    n1 = graph.visit(_model("http://shop.test/product/1", ["Add to cart"]))
    n2 = graph.visit(_model("http://shop.test/product/2", ["Add to cart"]))
    assert n1.fingerprint.key() == n2.fingerprint.key()
    assert len(graph.nodes) == 1
    assert n2.visit_count == 2


def test_different_element_signature_is_different_state():
    graph = StateGraph()
    graph.visit(_model("http://shop.test/product/1", ["Add to cart"]))
    graph.visit(_model("http://shop.test/product/1", ["Add to cart", "Write a review"]))
    assert len(graph.nodes) == 2


def test_mark_exercised_and_unexplored_states():
    graph = StateGraph()
    model = _model("http://shop.test/cart", ["Checkout", "Remove"])
    node = graph.visit(model)
    key = node.fingerprint.key()
    assert graph.unexplored_states() == [node]
    for el in model.elements:
        graph.mark_exercised(key, el.ref, element=el)
    assert graph.unexplored_states() == []


def test_mark_exercised_without_element_updates_refs_but_not_coverage():
    # Backward-compat path: omitting `element` still tracks exercised_refs (old
    # behavior) but does NOT reduce unexercised_signatures-based coverage — refs
    # alone aren't a stable enough signal for the (role, name) coverage report.
    graph = StateGraph()
    model = _model("http://shop.test/cart", ["Checkout"])
    node = graph.visit(model)
    key = node.fingerprint.key()
    graph.mark_exercised(key, "e0")  # no element passed
    assert "e0" in node.exercised_refs
    assert node.unexercised_element_count() == 1  # still uncovered by signature


def test_unreached_elements_lists_role_and_name():
    graph = StateGraph()
    model = _model("http://shop.test/cart", ["Checkout", "Remove"])
    node = graph.visit(model)
    key = node.fingerprint.key()
    checkout_el = next(e for e in model.elements if e.name == "Checkout")
    graph.mark_exercised(key, checkout_el.ref, element=checkout_el)

    unreached = graph.unreached_elements()
    assert len(unreached) == 1
    template, role, name = unreached[0]
    assert template == "http://shop.test/cart"
    assert role == "button"
    assert name == "Remove"


def test_unreached_elements_empty_when_fully_covered():
    graph = StateGraph()
    model = _model("http://shop.test/cart", ["Checkout"])
    node = graph.visit(model)
    key = node.fingerprint.key()
    for el in model.elements:
        graph.mark_exercised(key, el.ref, element=el)
    assert graph.unreached_elements() == []


def test_is_new_state_true_before_first_visit():
    graph = StateGraph()
    model = _model("http://shop.test/cart", ["Checkout"])
    assert graph.is_new_state(model) is True
    graph.visit(model)
    assert graph.is_new_state(model) is False


def test_coverage_summary_counts_are_consistent():
    graph = StateGraph()
    node = graph.visit(_model("http://shop.test/cart", ["Checkout", "Remove"]))
    graph.mark_exercised(node.fingerprint.key(), "e0")
    summary = graph.coverage_summary()
    assert summary["states"] == 1
    assert summary["total_elements"] == 2
    assert summary["exercised_elements"] == 1
    assert summary["unexplored_states"] == 1

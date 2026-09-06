"""Inference rules: synthetic ReconResult in, each rule's output asserted."""
from pathlib import Path

import pytest

from qaura.browser.observe import ElementInfo, PageModel
from qaura.init.infer import (
    ALWAYS_BLOCKED,
    build_inference,
    infer_admin_paths,
    infer_allowed_domains,
    infer_blocked_paths,
    infer_budgets,
    infer_personas,
    infer_rate,
    recon_digest,
)
from qaura.init.login import LoginWallSignal
from qaura.init.recon import ObservedState, ReconResult


def _state(url="http://app.example.com/", elements=None, key="k1"):
    model = PageModel(url=url, title="t", elements=elements or [])
    return ObservedState(
        key=key, url=url, template=url, title="t", status=200, depth=0,
        model=model, numerics=[], login_signal=LoginWallSignal(), html="<html></html>",
    )


def _result(**kwargs):
    defaults = dict(seed_url="http://app.example.com/", final_url="http://app.example.com/")
    defaults.update(kwargs)
    return ReconResult(**defaults)


def test_allowed_domains_uses_the_final_url_after_redirects():
    result = _result(seed_url="http://example.com/", final_url="https://www.example.com/home")
    domains, reason = infer_allowed_domains(result)
    assert domains == ["www.example.com"]
    assert "www.example.com" in reason


def test_allowed_domains_widens_only_for_same_site_subdomains():
    result = _result(final_url="https://app.example.com/",
                     subdomains_visited={"api.example.com"})
    assert infer_allowed_domains(result)[0] == ["example.com"]


def test_allowed_domains_does_not_widen_across_sites():
    result = _result(final_url="https://app.example.com/",
                     subdomains_visited={"cdn.othersite.net"})
    assert infer_allowed_domains(result)[0] == ["app.example.com"]


def test_logout_paths_are_always_blocked_even_if_never_seen():
    """Costs nothing when the route doesn't exist, and prevents a run from signing
    itself out and stranding everything after it on the login wall."""
    blocked, _ = infer_blocked_paths(_result(discovered_paths=["/"]))
    for pattern in ALWAYS_BLOCKED:
        assert pattern in blocked


def test_destructive_discovered_paths_are_blocked():
    blocked, _ = infer_blocked_paths(_result(
        discovered_paths=["/", "/account/delete", "/billing", "/products"]
    ))
    assert any("delete" in p for p in blocked)
    assert any("billing" in p for p in blocked)
    assert not any("products" in p for p in blocked)


def test_5xx_paths_are_kept_separate_not_blocked():
    """A 500 is a bug worth finding, not a boundary worth respecting."""
    blocked, low = infer_blocked_paths(_result(
        discovered_paths=["/", "/broken"], statuses={"/broken": 500, "/": 200},
    ))
    assert not any("broken" in p for p in blocked)
    assert any("broken" in p for p in low)


def test_admin_paths_are_empty_without_measurement():
    admin, low, measured = infer_admin_paths(_result(discovered_paths=["/", "/products"]))
    assert admin == [] and low == [] and measured is False


def test_admin_paths_guessed_from_names_stay_low_confidence():
    admin, low, measured = infer_admin_paths(_result(
        discovered_paths=["/", "/admin", "/admin/users", "/staff"]
    ))
    assert admin == []
    assert "/admin" in low and "/staff" in low
    assert measured is False


def test_admin_paths_are_high_confidence_once_measured():
    admin, low, measured = infer_admin_paths(_result(
        discovered_paths=["/", "/admin"],
        admin_measured={"/admin": "HTTP 403 anonymously"},
    ))
    assert admin == ["/admin"]
    assert low == []
    assert measured is True


def test_local_target_keeps_the_default_rate():
    rate, _ = infer_rate(_result(final_url="http://127.0.0.1:8099/"))
    assert rate == 5.0


def test_remote_target_with_rate_limit_signals_backs_right_off():
    rate, reason = infer_rate(_result(final_url="https://example.com/", saw_429=True))
    assert rate == 1.0
    assert "rate-limit" in reason


def test_fast_remote_target_gets_a_moderate_rate():
    rate, _ = infer_rate(_result(final_url="https://example.com/",
                                 nav_latencies_ms=[200.0, 300.0, 250.0]))
    assert rate == 2.0


def test_slow_remote_target_is_treated_conservatively():
    rate, _ = infer_rate(_result(final_url="https://example.com/",
                                 nav_latencies_ms=[2000.0, 2500.0]))
    assert rate == 1.0


@pytest.mark.parametrize("states,expected_actions", [
    (1, 100),      # clamped up from 25
    (8, 200),      # 25 * 8
    (40, 500),     # clamped down from 1000
])
def test_action_budget_clamps_at_both_ends(states, expected_actions):
    result = _result(states=[_state(key=f"k{i}") for i in range(states)])
    actions, wall_clock, llm_calls = infer_budgets(result, 5.0)
    assert actions == expected_actions
    assert 600 <= wall_clock <= 3600
    assert 100 <= llm_calls <= 500


def test_budget_chain_is_internally_consistent():
    result = _result(states=[_state(key=f"k{i}") for i in range(8)])
    actions, wall_clock, llm_calls = infer_budgets(result, 5.0)
    assert llm_calls == actions + 50
    assert wall_clock >= 600


def _textbox(name="Search"):
    return ElementInfo(ref="e1", role="textbox", name=name)


def _link(name="Next"):
    return ElementInfo(ref="e2", role="link", name=name)


def test_malicious_enabled_when_there_is_an_input():
    decisions = {d.name: d for d in infer_personas(_result(states=[_state(elements=[_textbox()])]))}
    assert decisions["malicious"].enabled


def test_malicious_disabled_on_a_static_page():
    decisions = {d.name: d for d in infer_personas(_result(states=[_state(elements=[])]))}
    assert not decisions["malicious"].enabled
    assert decisions["malicious"].reason  # the reason survives so it isn't a silent drop


def test_curious_and_accessibility_are_always_on():
    decisions = {d.name: d for d in infer_personas(_result(states=[_state(elements=[])]))}
    assert decisions["curious"].enabled
    assert decisions["accessibility"].enabled


def test_power_user_uses_link_density_not_raw_element_count():
    """A dozen buttons on a settings page is not a data table, so element count is the
    wrong proxy for the listing pages power_user exists to exercise."""
    buttons = [ElementInfo(ref=f"e{i}", role="button", name=f"b{i}") for i in range(14)]
    decisions = {d.name: d for d in infer_personas(_result(states=[_state(elements=buttons)]))}
    assert not decisions["power_user"].enabled

    links = [_link(f"item {i}") for i in range(20)]
    decisions = {d.name: d for d in infer_personas(_result(states=[_state(elements=links)]))}
    assert decisions["power_user"].enabled


def test_power_user_enabled_by_pagination():
    decisions = {d.name: d for d in infer_personas(
        _result(states=[_state(elements=[_link("Next"), _link("Previous")])])
    )}
    assert decisions["power_user"].enabled


def test_auth_roles_populated_when_a_wall_was_seen():
    result = _result(states=[_state()], login=LoginWallSignal(kind="full_wall", suggested_role="user"))
    inference = build_inference(result, Path.cwd())
    assert inference.auth_roles[0]["name"] == "user"
    assert inference.auth_roles[0]["storage_state_path"] == ".qaura/auth/user.json"


def test_auth_roles_empty_when_nothing_was_gated():
    inference = build_inference(_result(states=[_state()]), Path.cwd())
    assert inference.auth_roles == []


def test_admin_role_is_flagged_as_admin():
    result = _result(states=[_state()], role="admin")
    inference = build_inference(result, Path.cwd())
    assert inference.auth_roles[0]["is_admin"] is True


def test_repo_path_is_never_guessed_for_a_remote_target():
    result = _result(final_url="https://example.com/", states=[_state()])
    assert build_inference(result, Path.cwd()).repo_path is None


def test_recon_digest_is_stable_under_reordering():
    """Same digest means the crawl saw the same site, so a config diff is attributable
    to the model rather than to the crawl drifting."""
    a = _result(states=[_state(url="http://x/a", key="k1"), _state(url="http://x/b", key="k2")])
    b = _result(states=[_state(url="http://x/b", key="k2"), _state(url="http://x/a", key="k1")])
    assert recon_digest(a) == recon_digest(b)


def test_recon_digest_changes_when_the_site_changes():
    a = _result(states=[_state(url="http://x/a", key="k1")])
    b = _result(states=[_state(url="http://x/a", key="different")])
    assert recon_digest(a) != recon_digest(b)

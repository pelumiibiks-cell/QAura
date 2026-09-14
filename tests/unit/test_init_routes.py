"""robots.txt and sitemap.xml parsing, plus the read-only request policy.

The classify() tests are the read-only guarantee expressed without a browser. The live
counterpart is in test_init_readonly.py.
"""
import pytest

from qaura.browser.readonly import AllowOnce, classify
from qaura.config import GuardrailConfig
from qaura.init.recon import parse_robots, parse_sitemap, robots_blocks


def test_parse_robots_reads_the_wildcard_group():
    text = """
User-agent: *
Disallow: /admin
Disallow: /private/

Sitemap: https://example.com/sitemap.xml
"""
    disallowed, sitemaps = parse_robots(text)
    assert disallowed == ["/admin", "/private/"]
    assert sitemaps == ["https://example.com/sitemap.xml"]


def test_parse_robots_ignores_other_user_agent_groups():
    """A rule aimed at GPTBot says nothing about what a generic client may fetch, and
    honoring it would make recon stricter than the site actually asks."""
    text = """
User-agent: GPTBot
Disallow: /

User-agent: *
Disallow: /admin
"""
    disallowed, _ = parse_robots(text)
    assert disallowed == ["/admin"]


def test_parse_robots_strips_comments_and_blanks():
    disallowed, _ = parse_robots("User-agent: *  # everyone\nDisallow: /x  # secret\n\n")
    assert disallowed == ["/x"]


def test_parse_robots_on_empty_input():
    assert parse_robots("") == ([], [])


def test_parse_sitemap_extracts_locations():
    xml = """<?xml version="1.0"?>
<urlset><url><loc>https://example.com/a</loc></url>
<url><loc>  https://example.com/b  </loc></url></urlset>"""
    assert parse_sitemap(xml) == ["https://example.com/a", "https://example.com/b"]


def test_parse_sitemap_survives_malformed_xml():
    """A strict parser that raises on the whole document loses every URL in it over one
    bad entry, which is the common case in the wild."""
    xml = "<urlset><url><loc>https://example.com/a</loc></url><url><loc>oops"
    assert parse_sitemap(xml) == ["https://example.com/a"]


@pytest.mark.parametrize("path,blocked", [
    ("/admin", True), ("/admin/users", True), ("/administrator", True),
    ("/products", False), ("/", False),
])
def test_robots_blocks_by_prefix(path, blocked):
    assert robots_blocks(path, ["/admin"]) is blocked


def test_robots_disallow_root_blocks_everything():
    assert robots_blocks("/anything", ["/"])


def _cfg():
    return GuardrailConfig(allowed_domains=["example.com"], allowed_paths=["/**"])


class _Request:
    def __init__(self, method="GET", url="https://example.com/", navigation=False):
        self.method = method
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


def test_get_is_allowed():
    assert classify(_Request("GET", "https://example.com/a"), _cfg(), None) is None


@pytest.mark.parametrize("path", [
    "/logout.php", "/delete-account", "/signout.aspx", "/user/remove_item", "/LOG%4FUT",
])
def test_destructive_path_variants_are_blocked(path):
    # Regression: the pattern required "/", "?" or the end right after the word
    assert classify(_Request("GET", f"https://example.com{path}"), _cfg(), None) == "destructive path pattern"


@pytest.mark.parametrize("path", ["/cancellation-policy", "/removed-features", "/checkouts-report"])
def test_words_that_only_start_with_a_destructive_verb_are_allowed(path):
    assert classify(_Request("GET", f"https://example.com{path}"), _cfg(), None) is None


def test_destructive_action_in_the_query_string_is_blocked():
    request = _Request("GET", "https://example.com/account?id=4&action=delete")
    assert classify(request, _cfg(), None) == "destructive path pattern"


def test_allow_once_is_not_spent_by_an_analytics_beacon():
    allow = AllowOnce(origin="https://example.com", armed=True)
    beacon = _Request("POST", "https://example.com/collect")
    beacon.resource_type = "ping"
    assert classify(beacon, _cfg(), allow) is not None
    assert allow.matches(_Request("POST", "https://example.com/login"))


def test_head_is_allowed():
    assert classify(_Request("HEAD", "https://example.com/a"), _cfg(), None) is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_methods_are_blocked(method):
    reason = classify(_Request(method, "https://example.com/api"), _cfg(), None)
    assert reason and "non-read-only" in reason


def test_out_of_scope_navigation_is_blocked_before_it_leaves():
    """Blocking the request means the off-target host never receives the run's cookies,
    which enforce_scope_after_action() cannot achieve — by then the load already happened."""
    reason = classify(_Request("GET", "https://evil.test/", navigation=True), _cfg(), None)
    assert reason == "navigation out of scope"


def test_get_triggered_logout_is_blocked():
    """Rails' link_to method: :delete degrades to a GET, and 'click to unsubscribe' links
    are almost always GETs."""
    reason = classify(_Request("GET", "https://example.com/logout"), _cfg(), None)
    assert reason == "destructive path pattern"


@pytest.mark.parametrize("path", ["/logout", "/account/delete", "/orders/1/destroy", "/checkout"])
def test_destructive_get_paths_are_blocked(path):
    assert classify(_Request("GET", f"https://example.com{path}"), _cfg(), None) is not None


def test_allow_once_permits_exactly_one_post():
    allow = AllowOnce(origin="https://example.com", armed=True)
    request = _Request("POST", "https://example.com/login")
    assert classify(request, _cfg(), allow) is None

    allow.used = True
    assert classify(request, _cfg(), allow) is not None


def test_allow_once_does_nothing_until_armed():
    allow = AllowOnce(origin="https://example.com")
    assert classify(_Request("POST", "https://example.com/login"), _cfg(), allow) is not None


def test_allow_once_is_scoped_to_the_origin():
    """Same-origin, not any-origin: a login page that posts credentials off-site is not
    something to wave through."""
    allow = AllowOnce(origin="https://example.com", armed=True)
    assert classify(_Request("POST", "https://other.test/login"), _cfg(), allow) is not None


def test_allow_once_covers_a_fetch_to_a_different_endpoint():
    """SPA logins post to /api/session rather than the form's action, so an exact-URL
    exemption would block the exact case it exists to permit."""
    allow = AllowOnce(origin="https://example.com", armed=True)
    assert classify(_Request("POST", "https://example.com/api/session"), _cfg(), allow) is None

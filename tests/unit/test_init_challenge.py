"""Bot-protection detection.

Detection only. Nothing here tries to get past a challenge, and nothing should. What is
being prevented is subtler than "the crawl was blocked": without this, recon would treat
a Cloudflare interstitial as an ordinary page and emit a confident-looking config
describing a challenge screen.
"""
import pytest

from qaura.init.challenge import classify_challenge


def test_cloudflare_interstitial_is_detected():
    signal = classify_challenge(
        status=403, headers={"cf-ray": "8a1b2c3d4e5f"},
        title="Just a moment...",
        body_text="Checking your browser before accessing the site. Ray ID: 8a1b",
    )
    assert signal.detected
    assert signal.vendor == "Cloudflare"
    assert signal.status == 403


def test_managed_challenge_on_a_200_is_detected_by_its_element():
    """Cloudflare's managed challenge serves a 200 with the interstitial inline, so the
    status code alone would miss it."""
    signal = classify_challenge(
        status=200, headers={"cf-ray": "x"}, title="Please wait",
        body_text="", matched_selectors=["#cf-challenge-running"],
    )
    assert signal.detected


def test_datadome_block_is_detected():
    signal = classify_challenge(
        status=403, headers={"x-datadome": "protected"},
        title="Access denied", body_text="Request blocked. Please verify you are a human.",
    )
    assert signal.detected
    assert signal.vendor == "DataDome"


def test_imperva_reference_page_is_detected():
    signal = classify_challenge(
        status=403, headers={"x-iinfo": "1-2-3"},
        title="Access Denied",
        body_text="Incident ID: 0000-1111. Pardon our interruption.",
    )
    assert signal.detected


def test_akamai_503_is_detected():
    signal = classify_challenge(
        status=503, headers={"akamai-grn": "0.1a2b"},
        title="Access Denied", body_text="You have been blocked.",
    )
    assert signal.detected


def test_ordinary_page_behind_a_cdn_is_not_a_challenge():
    """Most of the internet is behind Cloudflare. A cf-ray on a 200 proves nothing, and
    treating it as a challenge would make the whole feature unusable on real sites."""
    signal = classify_challenge(
        status=200, headers={"cf-ray": "8a1b2c3d4e5f", "server": "cloudflare"},
        title="Acme Store — Cart",
        body_text="Your cart total is $42.00. Access your account to check out.",
    )
    assert not signal.detected


def test_a_page_merely_mentioning_access_denied_is_not_a_challenge():
    """A docs page about permissions is not a WAF block. The body marker only counts
    alongside a blocking status."""
    signal = classify_challenge(
        status=200, headers={},
        title="Permissions guide",
        body_text="If you see access denied, ask an administrator for the right role.",
    )
    assert not signal.detected


def test_plain_404_is_not_a_challenge():
    signal = classify_challenge(status=404, headers={}, title="Not found",
                                body_text="No such page.")
    assert not signal.detected


def test_ordinary_403_without_challenge_markers_is_not_flagged():
    """An app's own 403 is a finding for the security detector, not a reason to abort
    recon."""
    signal = classify_challenge(
        status=403, headers={}, title="Forbidden",
        body_text="You do not have permission to view this project.",
    )
    assert not signal.detected


@pytest.mark.parametrize("title", [
    "Just a moment...", "Attention Required! | Cloudflare", "Security check",
    "Are you a robot?", "DDoS-Guard",
])
def test_known_interstitial_titles(title):
    assert classify_challenge(status=200, headers={}, title=title, body_text="").detected


def test_describe_includes_the_evidence():
    signal = classify_challenge(
        status=429, headers={"cf-ray": "x"}, title="Just a moment",
        body_text="Checking your browser",
    )
    described = signal.describe()
    assert "Cloudflare" in described
    assert "429" in described

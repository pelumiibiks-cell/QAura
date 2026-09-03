from qaura.browser.recorder import NetworkEntry, Recorder
from qaura.detectors.network import detect


def _recorder() -> Recorder:
    return Recorder(page=None, context=None)


def _entry(**overrides) -> NetworkEntry:
    defaults = dict(
        url="http://x/api/thing", method="GET", status=None, ok=None,
        failure_text=None, resource_type="fetch", timestamp=0.0,
    )
    defaults.update(overrides)
    return NetworkEntry(**defaults)


def test_detects_4xx_xhr_failure():
    recorder = _recorder()
    recorder.network.append(_entry(status=404, ok=False, resource_type="xhr"))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert len(findings) == 1
    assert findings[0].detector == "network"
    assert "404" in findings[0].title


def test_detects_request_with_no_response():
    recorder = _recorder()
    recorder.network.append(_entry(status=None, ok=False, failure_text="net::ERR_CONNECTION_REFUSED"))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert len(findings) == 1
    assert "no response" in findings[0].title.lower()
    assert "ERR_CONNECTION_REFUSED" in findings[0].description


def test_ignores_non_xhr_fetch_resource_types():
    # A 404 on an image or a favicon is normal, not worth a finding — scoped to
    # requests the page's own logic actually depends on.
    recorder = _recorder()
    recorder.network.append(_entry(status=404, ok=False, resource_type="image"))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert findings == []


def test_ignores_successful_requests():
    recorder = _recorder()
    recorder.network.append(_entry(status=200, ok=True))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert findings == []


def test_does_not_double_count_5xx_already_owned_by_crash_detector():
    recorder = _recorder()
    recorder.network.append(_entry(status=500, ok=False))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert findings == []


def test_ignores_ok_none_with_a_status():
    # ok=None (a failed response READ, see recorder.py) is neither a confirmed
    # failure nor success — network.py only fires on an explicit ok=False.
    recorder = _recorder()
    recorder.network.append(_entry(status=404, ok=None))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert findings == []

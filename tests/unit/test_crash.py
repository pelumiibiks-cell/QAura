"""crash.py was one of three detectors (with console.py, network.py) advertised in
the README with zero tests — it's purely passive (reads what Recorder already
captured), so it doesn't need a real browser to test: populate a Recorder's buffers
directly with the same dataclasses browser/recorder.py's listeners append to.
"""
from qaura.browser.recorder import NetworkEntry, PageCrash, Recorder
from qaura.detectors.crash import detect


def _recorder() -> Recorder:
    # Real Recorder, constructed without ever attaching to a live Page/BrowserContext
    # — detect() only reads .crashes/.network, never touches ._page/._context.
    return Recorder(page=None, context=None)


def test_detects_uncaught_page_error():
    recorder = _recorder()
    recorder.crashes.append(PageCrash(message="TypeError: x is undefined", timestamp=0.0))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert len(findings) == 1
    assert findings[0].detector == "crash"
    assert findings[0].severity.value == "high"
    assert "TypeError" in findings[0].title


def test_detects_5xx_response():
    recorder = _recorder()
    recorder.network.append(NetworkEntry(
        url="http://x/api/save", method="POST", status=500, ok=False,
        failure_text=None, resource_type="fetch", timestamp=0.0,
    ))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert len(findings) == 1
    assert "500" in findings[0].title


def test_no_finding_for_successful_response():
    recorder = _recorder()
    recorder.network.append(NetworkEntry(
        url="http://x/api/save", method="POST", status=200, ok=True,
        failure_text=None, resource_type="fetch", timestamp=0.0,
    ))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert findings == []


def test_no_finding_for_4xx_response():
    # 4xx is network.py's job (a client-side failure), not crash.py's (server-side).
    recorder = _recorder()
    recorder.network.append(NetworkEntry(
        url="http://x/api/save", method="POST", status=404, ok=False,
        failure_text=None, resource_type="fetch", timestamp=0.0,
    ))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert findings == []


def test_multiple_crashes_and_5xx_all_reported():
    recorder = _recorder()
    recorder.crashes.append(PageCrash(message="err 1", timestamp=0.0))
    recorder.crashes.append(PageCrash(message="err 2", timestamp=0.0))
    recorder.network.append(NetworkEntry(
        url="http://x/a", method="GET", status=503, ok=False,
        failure_text=None, resource_type="xhr", timestamp=0.0,
    ))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert len(findings) == 3
    assert all(f.detector == "crash" for f in findings)


def test_repro_steps_and_persona_are_threaded_through():
    from qaura.reporting.models import ReproStep

    recorder = _recorder()
    recorder.crashes.append(PageCrash(message="boom", timestamp=0.0))
    steps = [ReproStep(description="click e1", action_kind="click", ref="e1")]
    findings = detect(recorder, "http://x/", steps, "malicious")
    assert findings[0].persona == "malicious"
    assert findings[0].repro_steps == steps

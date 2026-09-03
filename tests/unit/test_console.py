from qaura.browser.recorder import ConsoleEntry, Recorder
from qaura.detectors.console import detect


def _recorder() -> Recorder:
    return Recorder(page=None, context=None)


def test_detects_console_error():
    recorder = _recorder()
    recorder.console.append(ConsoleEntry(type="error", text="Uncaught ReferenceError", location=None, timestamp=0.0))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert len(findings) == 1
    assert findings[0].detector == "console"
    assert findings[0].severity.value == "medium"
    assert "ReferenceError" in findings[0].title


def test_ignores_non_error_console_messages():
    recorder = _recorder()
    recorder.console.append(ConsoleEntry(type="log", text="debug info", location=None, timestamp=0.0))
    recorder.console.append(ConsoleEntry(type="warning", text="deprecation notice", location=None, timestamp=0.0))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert findings == []


def test_includes_location_when_present():
    recorder = _recorder()
    recorder.console.append(ConsoleEntry(type="error", text="boom", location="app.js:42", timestamp=0.0))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert "app.js:42" in findings[0].description


def test_multiple_errors_all_reported():
    recorder = _recorder()
    for i in range(3):
        recorder.console.append(ConsoleEntry(type="error", text=f"err {i}", location=None, timestamp=0.0))
    findings = detect(recorder, "http://x/", [], "heuristic")
    assert len(findings) == 3

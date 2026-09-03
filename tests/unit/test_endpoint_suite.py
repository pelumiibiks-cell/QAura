"""Live tests against tests/fixtures/buggy_app's /api/validate-email — same
skip-if-not-running pattern as test_replay.py. Also includes tests against a local
httpx.MockTransport for cases that don't need a real server (timeout, connection
error, nondeterminism, latency threshold) — faster and doesn't depend on the fixture
for those specific behaviors.
"""
import httpx
import pytest

from qaura.mltest.suites.endpoint import probe_endpoint

FIXTURE_URL = "http://127.0.0.1:8099/api/validate-email"


def _fixture_is_up() -> bool:
    try:
        httpx.get("http://127.0.0.1:8099/", timeout=1.0)
        return True
    except Exception:
        return False


requires_fixture = pytest.mark.skipif(not _fixture_is_up(), reason="buggy_app fixture not running on :8099")


@requires_fixture
async def test_probe_finds_real_crashes_on_buggy_app():
    result = await probe_endpoint(FIXTURE_URL, base_payload={"email": "user@example.com"}, repeat_count=2)
    detectors = {f.detector for f in result.findings}
    assert "ml_endpoint" in detectors
    # at least the null_values and wrong_type cases are known real 500s on this fixture
    titles = " ".join(f.title for f in result.findings)
    assert "null_values" in titles or "wrong_type_for_all_fields" in titles


@requires_fixture
async def test_probe_records_real_latencies():
    result = await probe_endpoint(FIXTURE_URL, base_payload={"email": "user@example.com"}, repeat_count=3)
    assert len(result.latencies_ms) == 3
    assert all(l >= 0 for l in result.latencies_ms)


@requires_fixture
async def test_probe_no_determinism_finding_for_stable_endpoint():
    # validate-email is a pure function of its input -- same input, same output, every time
    result = await probe_endpoint(FIXTURE_URL, base_payload={"email": "stable@example.com"}, repeat_count=4)
    assert not any("Nondeterministic" in f.title for f in result.findings)


# --- mocked-transport tests: don't need the fixture running -----------------------

_RealAsyncClient = httpx.AsyncClient  # captured before any monkeypatching below


def _make_client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)
    return factory


async def test_probe_detects_nondeterministic_response(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # alternate response bodies for the repeated identical-input requests
        return httpx.Response(200, json={"result": call_count["n"] % 2})

    monkeypatch.setattr(httpx, "AsyncClient", _make_client_factory(handler))
    result = await probe_endpoint("http://fake/predict", base_payload={"x": 1}, repeat_count=4)
    assert any("Nondeterministic" in f.title for f in result.findings)


async def test_probe_no_nondeterminism_finding_for_stable_mock(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "always the same"})

    monkeypatch.setattr(httpx, "AsyncClient", _make_client_factory(handler))
    result = await probe_endpoint("http://fake/predict", base_payload={"x": 1}, repeat_count=4)
    assert not any("Nondeterministic" in f.title for f in result.findings)


async def test_probe_detects_5xx_on_malformed_input(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.content == b"" or b"null" in request.content or b"999999" in request.content:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "AsyncClient", _make_client_factory(handler))
    result = await probe_endpoint("http://fake/predict", base_payload={"x": "a"}, repeat_count=1, check_determinism=False)
    assert any(f.detector == "ml_endpoint" and "500" in f.title for f in result.findings)


async def test_probe_flags_high_latency(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "AsyncClient", _make_client_factory(handler))
    # threshold of -1ms guarantees every real (even near-zero) latency exceeds it
    result = await probe_endpoint(
        "http://fake/predict", base_payload={"x": 1}, repeat_count=2,
        latency_p95_threshold_ms=-1, check_determinism=False,
    )
    assert any("High latency" in f.title for f in result.findings)

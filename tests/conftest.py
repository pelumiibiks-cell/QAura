"""Session-wide test setup: starts tests/fixtures/buggy_app on :8099 automatically,
for the tests that need a live server (test_replay.py, test_endpoint_suite.py,
test_genai_suite.py), if nothing is already listening there.

This has to happen at CONFTEST IMPORT time, not inside a fixture — those three test
modules decide whether to skip via a module-level `pytestmark =
pytest.mark.skipif(not _fixture_is_up(), ...)`, which pytest evaluates at COLLECTION
time, before any fixture (even a session-scoped autouse one) would ever run. Starting
the server from a fixture would always be one step too late: collection would already
have decided to skip by the time the fixture got a chance to run. Module-level code in
the rootdir conftest.py runs before collection, so the server is live by the time
those skipif checks execute.

Without this, tests that depend on a live server silently skip on a fresh clone or in
CI unless someone remembers to start the fixture by hand first — which means a CI run
can report fully green while never actually exercising replay, the mechanism every
emitted repro script depends on.
"""
from __future__ import annotations

import atexit
import threading
import time

_FIXTURE_URL = "http://127.0.0.1:8099/"
_server = None
_thread: threading.Thread | None = None


def _is_up() -> bool:
    try:
        import httpx
        httpx.get(_FIXTURE_URL, timeout=1.0)
        return True
    except Exception:
        return False


def _start_fixture_server() -> None:
    global _server, _thread
    if _is_up():
        return  # already running -- started manually, or left up by a prior
        # session; don't fight over the port, just use what's there

    try:
        import uvicorn
        from tests.fixtures.buggy_app.app import app
    except ImportError:
        # fastapi/uvicorn aren't installed (missing the `dev` extra) -- leave it be,
        # the individual test modules' own _is_up()-equivalent checks will see the
        # fixture as down and skip, exactly as they did before this conftest existed.
        return

    config = uvicorn.Config(app, host="127.0.0.1", port=8099, log_level="warning")
    _server = uvicorn.Server(config)
    _thread = threading.Thread(target=_server.run, daemon=True)
    _thread.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _is_up():
            return
        time.sleep(0.1)
    # Didn't come up within the deadline -- same fallback as the ImportError case.


def _stop_fixture_server() -> None:
    if _server is not None:
        _server.should_exit = True
    if _thread is not None:
        _thread.join(timeout=5)


_start_fixture_server()
atexit.register(_stop_fixture_server)

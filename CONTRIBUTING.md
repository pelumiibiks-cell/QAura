# Contributing

## Setup

```
pip install -e ".[dev,ml]"
playwright install chromium
```

## Tests

```
pytest
```

Some tests need a real Chromium (via Playwright) and the `ml` extra; both are covered by `pip install -e ".[dev,ml]"` above. A handful of tests (replay, the endpoint suite, the GenAI suite) additionally need `tests/fixtures/buggy_app` running on `:8099` — start it with:

```
uvicorn tests.fixtures.buggy_app.app:app --port 8099
```

Those tests skip cleanly if it isn't running, but skipped isn't the same as passing — run the fixture before trusting a green run touches the replay path.

## Before opening a PR

- `pytest` passes.
- New behavior has a test that would fail without the fix.
- `qaura doctor` if you touched anything under `qaura/llm/`.

## Guardrails and safety

This is a tool that drives a real browser against a real target and can hold live authenticated sessions. Changes to `qaura/core/guardrails.py` (scope checks, destructive-action blocking) get read carefully — a false negative there is a real safety regression, not just a bug.

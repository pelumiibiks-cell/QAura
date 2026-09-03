# QAura

An autonomous QA testing agent. Point it at a web app and it behaves like a human QA
engineer: explores the app, clicks around, tries unusual inputs, finds broken flows
and visual defects, catches crashes, checks business-logic invariants a crash-only
scanner would never catch, and writes reproducible bug reports — with a likely
responsible source file, a suggested fix, a real reproducibility rate from replaying
the bug, an emitted regression test, and a screenshot. Five personas (curious,
impatient, malicious, power user, accessibility) explore the same app differently,
either with a deterministic no-LLM crawler or Gemini-driven reasoning.

A second half tests trained ML models, live inference endpoints, GenAI features, and
the data feeding them: fairness/calibration/robustness/regression checks against a
trained model, adversarial probing of a live endpoint, and injection/leakage/PII
probing of a chat-shaped AI feature.

Status: all seven build phases complete and live-verified against a deliberately
buggy fixture app (`tests/fixtures/buggy_app`) and a deliberately degraded ML model
(`tests/fixtures/ml`) — not just written, actually run and confirmed to catch real,
seeded bugs. See `docs/PROGRESS.md` for the full build history, every real bug found
along the way (in QAura itself, not just the targets it tests), and what's still
worth improving.

**Known limitation:** the LLM-persona crawl path (`--personas`) has not yet received
the form-awareness and frontier-navigation work that the deterministic heuristic
crawler has — a persona run today explores noticeably more shallowly than heuristic
mode. See `HOW_IT_WORKS.md` for detail. `--no-llm` (heuristic) mode is unaffected.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,ml]"
playwright install chromium
```

Copy `.env.example` to `.env` and set `GEMINI_API_KEY`. Without a key, QAura runs in
heuristic (no-LLM) mode — the crawler, every detector, invariants, dedupe, replay,
localization, and repro-script generation all work with zero API spend. An LLM key
adds persona-driven exploration, expectation-divergence detection, LLM triage, and
suggested fixes.

## Quick start

```
qaura doctor                             # verify the Gemini key and reachable models

qaura observe <url>                      # print the distilled page model for a URL
qaura auth capture --url <url> --role user   # save a login session for reuse

qaura run --url <url> --no-llm           # heuristic crawl, zero API spend
qaura run --url <url> --personas curious,impatient
qaura run --url <url> --no-llm --ci --fail-on high   # CI gate: exit 1 on high+ findings

qaura report runs/<timestamp>            # regenerate HTML from a saved run
qaura replay runs/<timestamp>            # re-confirm reproducibility for a saved run

qaura ml test --model model.joblib --data eval.csv --slice-col group --baseline prior.joblib
qaura ml probe --endpoint http://localhost:8000/predict --payload '{"x": 1}'
qaura ml genai --url <url> --input-ref e1 --send-ref e2 --response-selector "#chat-response"
```

## What a run actually does

`qaura run` drives a real headless (or headed, with `--headed`) browser through the
target app — either a deterministic heuristic crawler (breadth-first over
never-tried elements, fuzzing text fields with a curated corpus of boundary,
encoding, injection, and format-mismatch values) or one or more Gemini-driven
personas that reason about what to try next and state a checkable expectation for
each action. After every action, it checks for crashes, console errors, failed
network requests, declarative business-logic invariants (`qaura.yaml`), reflected
injection, and performance regressions; visual and accessibility checks run once per
distinct page state. Findings then go through dedupe (near-duplicates merged), LLM
triage (severity double-checked, confident false positives dropped — LLM mode
only), source localization (candidate files via a repo-relative substring search —
needs `repo_path` set), replay (each finding re-executed 2+ times against a fresh
browser to confirm it's really reproducible, not a fluke), and repro-script
emission (a runnable pytest file per finding — proven live to fail against a buggy
app and pass against the fixed one). The report is one self-contained HTML file:
severity-badged findings, a coverage summary naming exactly which elements were
never reached, and embedded screenshots.

## Project layout

```
qaura/
  cli.py               all commands: run, doctor, observe, auth, replay, report, ml test/probe/genai
  config.py             qaura.yaml + .env loading (see qaura.example.yaml)
  llm/                  Gemini provider (Interactions API), budget tracking, structured-output schemas
  browser/               Playwright driver, page observation, actions, auth, evidence recording
  core/                  state graph, guardrails, the heuristic crawler, the LLM-driven orchestrator,
                         the planner, the invariants engine, the fuzz-input corpus
  personas/              five persona system prompts
  detectors/             crash, console, network, visual, a11y, security, performance, flow (divergence)
  analysis/              dedupe, LLM triage, source localization, fix suggestions, replay, visual baselines
  reporting/              Finding/RunReport models, the HTML report, repro-script generation
  mltest/                 model registry + four suites: artifact, data/drift, endpoint, genai
tests/
  fixtures/buggy_app/     a deliberately buggy FastAPI + JS app — every detector proven against it
  fixtures/ml/            a deliberately degraded classifier + baseline — every ML check proven against it
```

Guardrails (domain/path allowlist, destructive-action gating, payment/email test-value
substitution, rate and budget caps) execute in code before every single action,
independent of what any persona's prompt says — see `core/guardrails.py`.

Full architecture, every design decision and why, and the complete build history
(including real bugs found in QAura itself along the way, not just in the apps it
tests): `docs/PROGRESS.md`.

### Environment variables

Every config field is also settable via an env var prefixed `QAURA_` (e.g.
`QAURA_TARGET_URL`, `QAURA_SEED`, `QAURA_OUTPUT_DIR`) — handy for CI, where a
`qaura.yaml` per environment is more friction than it's worth. `GEMINI_API_KEY` is the
one exception; it's read unprefixed, since that's the name `.env`/`google-genai`
itself expects.

`.env` and `qaura.yaml` are both resolved against the current working directory, not
the project root — running `qaura` from a subdirectory silently drops the API key
(falling back to heuristic mode) and any custom config. Same for a captured auth
session (`.qaura/auth/<role>.json`): run from the wrong directory and `--role` quietly
falls back to unauthenticated instead of erroring.

### A note on `qaura ml test`

Loading a model file (`.pkl`/`.joblib` via `joblib.load()`, `.pt`/`.pth` via
`torch.load(weights_only=False)`) deserializes a Python pickle, which can execute
arbitrary code embedded in the file. Only point `qaura ml test`/`qaura ml probe` at
model artifacts you trust — see `SECURITY.md`.

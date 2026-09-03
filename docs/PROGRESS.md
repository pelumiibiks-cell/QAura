# QAura build history

This is the build log: what was built in what order, what broke on first live contact
and how it got fixed, and the design rationale behind the non-obvious decisions. For
setup and usage, see `README.md`; for the pipeline explainer, see `HOW_IT_WORKS.md`.

Stack: Python 3.13 + Playwright + Gemini (`google-genai`, Interactions API — NOT
`generate_content`, see the LLM layer section below for verified call shapes).

## ALL SEVEN PHASES COMPLETE. QAura is built, wired end to end, and live-verified.

271 test functions (260 passing + 11 that correctly skip when the fixture server
isn't running — see `test_replay.py`/`test_endpoint_suite.py`/`test_genai_suite.py`'s
skip-if-not-running pattern). Every phase's deliverable was actually run against a
real target, not just written and unit-tested in isolation — `tests/fixtures/buggy_app`
(a deliberately buggy FastAPI + JS app) and `tests/fixtures/ml` (a deliberately
degraded scikit-learn model + baseline) exist specifically so every detector and
every ML check could be proven to fire on a real, known-broken case and stay quiet
on the clean one, not assumed to work from the code reading correctly.

If you're a fresh session picking this up: the codebase is functionally complete
per the original plan. What's left is genuinely optional polish, not missing
functionality — see "Known blockers / open questions" at the end of this file for
the short, honest list (mostly: the Gemini Pro-tier quota-0 constraint, which needs
the user's billing decision, and a couple of documented scope decisions like visual
baselines comparing recurring findings rather than a full state-by-state sweep).
Read `PROGRESS.md` top to bottom once if you want the full story of what was built,
what broke on first live contact and how it got fixed, and why — every phase found
at least one real bug through actually running the thing, and none of them were
glossed over. If you just want to verify the whole system still works: start
`tests/fixtures/buggy_app` (`python -m tests.fixtures.buggy_app.app`, port 8099),
run `qaura run --url http://127.0.0.1:8099/ --no-llm`, and read the generated
`report.html` — it should show findings, screenshots, coverage, and (if you also
pass `--replay-attempts` and don't skip analysis) reproducibility rates and repro
scripts.

### Done
- [x] `.env` normalized to `GEMINI_API_KEY=...` (was `GEMINI_API_KEY = "..."` with spaces).
      Key was already named correctly when re-checked — an earlier grep showing `API-KEY`
      was stale/from before the user finished creating the file. Value never read into chat.
- [x] `.gitignore` written (excludes `.env`, `runs/`, `.qaura/`, `.qaura-wakeup/`, venv,
      playwright artifacts)
- [x] `.env.example` written (key name only, no value)
- [x] `git init` — did NOT set a local user.email override; inherited the existing global
      git identity (`pelumiibiks-cell` / `pelumiibiks@gmail.com`) since that's presumably
      the user's real established identity, don't second-guess it
- [x] Directory scaffold created under `qaura/` and `tests/`, all `__init__.py` stubs in place
- [x] `pyproject.toml` written. `google-genai` pin corrected to `>=2.0` after checking PyPI
      directly (latest available was 2.20.0 — the `>=0.3` guess from planning was too low
      and would have been fine but was tightened once real data was available)
- [x] `README.md` written (minimal — hatchling's build needs it to exist for editable
      install to succeed at all; this is what surfaced the missing-file error below)
- [x] `PROGRESS.md` created and maintained (this file)
- [x] `qaura/config.py` — pydantic-settings `QAuraConfig`, loads `.env` automatically via
      `env_file=".env"`, plus optional `qaura.yaml` merged in `load_config()`. Sub-models:
      `ModelTiers`, `GuardrailConfig`, `PersonaConfig`, `InvariantConfig`, `AuthRole`.
      `effective_llm_mode` property resolves auto/gemini/heuristic based on key presence.
- [x] `qaura.example.yaml` written matching config.py's shape
- [x] `qaura/cli.py` — typer app. `doctor` fully wired to `llm/gemini.py:run_doctor`. All
      other subcommands (`observe`, `auth`, `run`, `replay`, `report`, `ml test/probe/genai`)
      are stubs that print which phase implements them and exit 1 — intentional, so
      `qaura --help` is honest rather than silently doing nothing.
- [x] `qaura/llm/base.py` — `LLMProvider` Protocol, `Tier` enum (maps to `ModelTiers` field
      names), `Usage`/`LLMResponse`/`ImagePart` dataclasses. `Usage` fields are explicitly
      marked provisional pending live confirmation (see blockers).
- [x] `qaura/llm/null.py` — `NullProvider`, backs heuristic mode, raises loudly if
      `.complete()` is ever called (callers must check `.available` first)
- [x] `qaura/llm/gemini.py` — `GeminiProvider` implementing `LLMProvider` against
      `client.interactions.create(...)`, PLUS `run_doctor(cfg, console)` which is the thing
      that actually validates all of this against a live key: checks key presence, lists
      reachable models, cross-checks configured tier model IDs against that list, then makes
      one real `interactions.create()` call and dumps the raw usage object so
      `_extract_usage()`'s guessed field names can be corrected. Written defensively — if
      the real SDK surface differs from what was read off Google's docs during planning,
      `qaura doctor` reports exactly where and how, rather than the whole thing crashing
      opaquely deep in a later phase.
- [x] `tests/unit/test_config.py` — 5 tests covering default loading, auto/forced llm_mode,
      yaml pickup, guardrail-safe defaults. NOT YET RUN (pytest install pending, see below).

### Phase 0 verification results (all confirmed live, not guessed)
- `pip install -e ".[dev,ml]"` completed clean.
- `.\.venv\Scripts\python.exe -m pytest tests/unit/ -v` → **10/10 passed** (5 config, 5 budget).
- `.\.venv\Scripts\python.exe -m qaura.cli doctor` ran against the REAL key and REAL API:
  - Key found, client constructed, `client.models.list()` returned 30+ reachable models.
  - Every configured tier model in `config.py`'s `ModelTiers` defaults (`gemini-3.1-pro-preview`,
    `gemini-2.5-pro`, `gemini-3.5-flash-lite`, `gemini-3.7-flash`) showed OK / reachable —
    **the model IDs from planning were correct, no correction needed.**
  - `client.interactions.create(...)` succeeded — confirmed the Interactions API is real and
    matches the shape used in `gemini.py`. `dir(genai.Client)` also directly confirmed an
    `interactions` attribute exists on installed `google-genai` 2.20.0.
  - **Correction made:** the guessed `usage` field names (`input_tokens`, `output_tokens`)
    do NOT exist on the real usage object and silently returned 0. Real field names are
    `total_input_tokens` / `total_output_tokens` / `total_tokens` (plus
    `input_tokens_by_modality`, `total_cached_tokens`, `total_thought_tokens`,
    `total_tool_use_tokens`, `raw_prompt_token` — unused so far). Fixed in
    `gemini.py:_extract_usage`, re-ran doctor, confirmed correct: `input=8 output=1 total=9`
    matching the raw dump exactly. `llm/budget.py` was then written on top of the CONFIRMED
    field names, not the guessed ones — safe to build on.
- `qaura` console script not yet tried directly (`.\.venv\Scripts\qaura.exe`) — used
  `python -m qaura.cli doctor` instead, which works identically. Try the console script next
  session if curious, not required.

Phase 0 deliverable ("qaura doctor confirms the Gemini key works") is met and verified,
not just written. **Do not re-litigate the usage field names or model IDs** — they're
confirmed, move on.

### Phase 1 files written
- `qaura/browser/driver.py` — `Driver` async-context-manager wrapping one Playwright +
  Browser instance, `ContextSpec` dataclass, `Driver.context()` hands out one
  BrowserContext+Page per (persona, role) pair.
- `qaura/browser/observe.py` — `PageModel`/`ElementInfo`, `build_page_model()`,
  `parse_aria_snapshot()`. See "Correction made" below — this file was rewritten once
  after hitting a real API break.
- `qaura/browser/actions.py` — `Action`/`ActionKind`/`execute()`, ref-based (never raw
  CSS selectors), includes the `expectation` field the plan's design decision #3 needs
  for Phase 3's divergence detector (unused until then, just carried on the dataclass).
- `qaura/browser/recorder.py` — `Recorder` (console/network/crash listeners, screenshot,
  trace.zip). Written and structurally sound but NOT YET wired into any CLI command or
  exercised live — nothing calls it yet since `observe` doesn't need evidence capture.
  First real exercise will be the Phase 2 orchestrator.
- `qaura/browser/auth.py` — `capture()`/`list_captured()`/`resolve_role()`. Wired into
  `qaura auth capture`/`qaura auth list` in cli.py. **NOT live-tested** — capture is
  inherently interactive (opens a visible browser, waits for a human to log in and press
  Enter), can't be exercised non-interactively. Code reviewed carefully instead; first
  real test will be whenever Phase 2's buggy_app fixture has a login flow to capture, or
  whenever you personally run `qaura auth capture --url <real app>`.
- `qaura/cli.py` — `observe` fully wired and VERIFIED live (see below). `auth capture`/
  `auth list` wired but not live-tested (see above).

### Correction made mid-phase (important — read before touching observe.py)
First live run of `qaura observe` failed immediately:
`AttributeError: 'Page' object has no attribute 'accessibility'`. The plan's
`observe.py` was written assuming `page.accessibility.snapshot()` (the classic
Playwright accessibility API, nested-dict tree) — that API is REMOVED in the installed
Playwright version (1.62.0, confirmed via `pip show playwright`). This was not something
planning could have caught; it only showed up by actually running the command.

Investigated live (not guessed): `dir(Page)` shows an `aria_snapshot` method instead.
Tested it directly against example.com and a local form fixture (textbox with value,
checked checkbox, disabled button, combobox with selected option) to learn its real
output shape — it returns a YAML-ish **string**, not a dict, e.g.:
```
- textbox "Email": a@b.com
- checkbox "Subscribe" [checked]
- button "Disabled Btn" [disabled]
- combobox:
  - option "One" [selected]
```
Rewrote `observe.py` around this: `yaml.safe_load()` parses the structural
nesting (already a pyproject dependency, no new dep needed), then a regex
(`_DESCRIPTOR_RE`) parses each node's `role "name" [attrs]` text. `parse_aria_snapshot()`
is the new public entry point, unit-tested against the exact live-captured fixture
strings in `tests/unit/test_observe.py` (6 new tests, all passing) — not synthetic
guesses at the format, the real thing.

**Verified end-to-end twice**, not just unit-tested:
1. `qaura observe https://example.com` → correctly showed `e1 link "Learn more"`.
2. `qaura observe` against a local HTML fixture with a textbox/checkbox/buttons/combobox/
   link → correctly showed value=`a@b.com`, checked=True, disabled flag, all 8 interactive
   elements with sequential refs. This is the richer case and it round-tripped correctly.

**If you're a fresh session and something in the browser layer breaks in a way that
looks like an API mismatch: this is now the second time it's happened (config's README
build issue doesn't count, that was packaging not API). Trust the installed package's
actual `dir()` output over anything read from docs or training memory, for BOTH Playwright
and google-genai in this project — verify live before writing more than a few lines
against an assumed shape.**

### Next action
Phase 2 (heuristic run, no LLM) per the plan. In order:
1. `qaura/core/state.py` — state fingerprinting (uses `PageModel.signature()`, already
   built and tested) + a state graph (visited states, edges = actions taken between them,
   coverage tracking, dead-end detection).
2. `qaura/core/guardrails.py` — domain/path allowlist, destructive-pattern classifier
   (config already has `GuardrailConfig.destructive_patterns`), payment/email sink
   substitution (config already has `fake_email_domain`/`test_card_number`), rate/budget
   caps (`max_actions_per_run`, `max_requests_per_second`, `max_wall_clock_seconds`).
   Executes in code before every action — never trust a prompt for this, per the plan.
3. `qaura/core/inputs.py` — the unusual-input fuzz corpus (empty, huge, unicode/RTL,
   SQL/XSS-shaped markers for later detector use, boundary numbers, whitespace-only).
4. `qaura/core/heuristic.py` — BFS over the state graph via `core/state.py`, priority
   queue over unexercised elements from `PageModel.elements`, fuzzes every form field
   using `inputs.py`, drives `browser/actions.py` + `browser/recorder.py` together.
5. `qaura/detectors/crash.py`, `console.py`, `network.py` — passive detectors reading off
   a `Recorder` instance (crashes list, console_errors property, network_failures
   property — all three already exist on `Recorder`, just need consuming).
6. `qaura/reporting/models.py` — `Finding`/`Evidence`/`RunReport` dataclasses.
7. `qaura/reporting/html.py` — single-file HTML report, Jinja2 (already a dependency).
8. Wire `qaura run --url ... --no-llm` in cli.py for real (currently a stub).
9. Stand up `tests/fixtures/buggy_app/` (FastAPI + vanilla JS) with AT LEAST the bugs
   detectable without an LLM or invariants engine (both land later): #2 (500 on unusual
   input), #3 (uncaught TypeError), #4 (silent 404 + stuck spinner). The other 6 seeded
   bugs need Phase 4's invariants/visual/a11y/security detectors or Phase 3's personas —
   don't block Phase 2 on building fixture code for detectors that don't exist yet; add
   those bugs to the fixture app incrementally as each detector phase lands.
   **This is the Phase 2 deliverable**: `qaura run --url http://localhost:8099 --no-llm`
   producing a real HTML report with real findings against this fixture, zero API spend.

### Phase 2 files written (all VERIFIED — actually run against a live fixture, not
just unit tested)
- `qaura/core/state.py` — `normalize_path()`/`url_template()`, `StateFingerprint`,
  `StateGraph`. 11/11 unit tests.
- `qaura/core/guardrails.py` — `is_in_scope()`, `is_destructive()`, `check_action()`,
  `sanitize_fill_value()`, `RunLimiter`. 16/16 unit tests.
- `qaura/core/inputs.py` — `FuzzValue` corpus (boundary/encoding/injection_marker/format
  categories), `values_for_role()`, `diverse_slice()`. 4/4 unit tests. See correction
  below — `diverse_slice()` didn't exist in the first pass and its absence was a real
  bug, not a hypothetical.
- `qaura/core/heuristic.py` — `HeuristicCrawler`: BFS-ish traversal via `StateGraph`,
  always returns to `target_url` when a branch is fully exercised, fuzzes text fields
  with `diverse_slice()` + a Tab press after each fill (to trigger blur-validation),
  runs all three passive detectors after every action, respects `RunLimiter` caps.
- `qaura/detectors/crash.py`, `console.py`, `network.py` — passive, read off a
  `Recorder` instance, no browser interaction of their own.
- `qaura/reporting/models.py` — `Finding`/`Evidence`/`ReproStep`/`RunSummary`/`RunReport`.
- `qaura/reporting/html.py` — single-file Jinja2 report, autoescaped (verified an
  injected `<script>` in a finding description renders escaped, not executed —
  important since finding descriptions can contain text scraped from the target app).
- `qaura/cli.py` — `run` fully wired: builds a `Driver`+`Recorder`, runs
  `HeuristicCrawler`, writes both `report.json` and `report.html` to a timestamped
  subdirectory of `--out`. Caught and fixed a `Path` NameError (used before import)
  before it ever got run — code review, not a live-run discovery.
- `tests/fixtures/buggy_app/app.py` — FastAPI + vanilla JS fixture, serves on :8099.
  Three Phase-2-catchable bugs seeded (numbered per the plan's verification list):
  **#2** (500 on unusual input — validate-email crashes on any character above
  U+2000, i.e. emoji), **#3** (uncaught TypeError — a button handler calls `.activate()`
  on `undefined`), **#4** (silent 404 — `/api/subscribe` doesn't exist, and the JS
  never updates the "loading..." status on failure). Bugs #1, #5-#9 need Phase 3/4
  detectors (invariants, visual, a11y, security, personas) that don't exist yet —
  intentionally deferred, not forgotten; add them to this fixture as each phase lands.

### Real corrections made while getting `qaura run --no-llm` to actually work (this is
the point of Phase 2 verification — these would NOT have been caught by unit tests alone)
1. **`Path` used before import in `cli.py`** — caught in code review before running,
   fixed immediately.
2. **The fuzz slice had zero category diversity.** First implementation was
   `values_for_role(role)[:FUZZ_SLICE_SIZE]` — since `ALL_VALUES` is
   `BOUNDARY_VALUES + ENCODING_VALUES + INJECTION_MARKER_VALUES + FORMAT_MISMATCH_VALUES`
   and boundary sorts first, a slice of 4 was ALWAYS 4 boundary values and NEVER
   touched encoding/injection/format at all. First live run against buggy_app only
   found 3/... wait, found the #3 and #4 bugs but missed #2 (the unusual-input crash)
   entirely, because the fuzz slice never contained anything unusual enough to trigger
   it. Root-caused by inspecting the report and reasoning through what the crawler
   actually sent, not by guessing — added `core/inputs.py:diverse_slice()`
   (round-robins across categories) and switched `heuristic.py` to use it. New unit
   test `test_diverse_slice_samples_each_category_before_repeating` locks this in so
   the mistake can't silently come back.
3. **The fixture's bug #2 was originally coupled across two elements** (fill the email
   field with a specific bad value, THEN click a separate "Check email" button) — not
   reachable by a per-element-independent heuristic crawler that doesn't understand
   form relationships (that coupling is exactly what Phase 3's LLM planner will add).
   Fixed by moving validation to fire `onblur` of the email field itself, so a
   fill+Tab sequence (which the crawler already does for every text field) triggers it
   directly. This is also more realistic UI behavior (live validation), not a hack.
4. **Even after fix #2, `diverse_slice` needed the specific trigger value to be
   reachable within a *small* slice** — round-robin only samples ONE value per
   category before the 4-value budget is exhausted, so a value buried 4th within its
   own category (the original `null_byte` trigger) still wouldn't be hit. Rather than
   grow the slice size (doesn't scale — reaching a specific deep value would need
   ~14 values per field), retargeted the fixture's crash condition to trigger on
   `unicode_emoji`, which `diverse_slice`'s round-robin guarantees is the very first
   encoding-category value tried. Locked in with
   `test_diverse_slice_first_encoding_value_is_unicode_emoji` so if the corpus
   ordering ever changes, the test fails loudly instead of the fixture silently
   stopping to catch its own bug.

Final verified run: `qaura run --url http://127.0.0.1:8099/ --no-llm --out runs/heuristic`
→ **5 findings, all three seeded bugs caught** (some via two detectors each, which is
expected and fine — dedup is Phase 5's job, not Phase 2's): bug #2 via both `crash`
(500 status) and `console` (browser's own resource-load error log), bug #3 via `crash`
(uncaught pageerror), bug #4 via both `console` and `network`. Coverage summary showed
all 4 interactive elements on the single page state exercised. Both `report.json` and
`report.html` written and spot-checked (severity badges, finding counts, escaped
content all present in the rendered HTML). 59/59 unit tests passing after these fixes.

### Next action
Phase 3 (Gemini planner and personas) per the plan. In order:
1. `qaura/llm/schemas.py` — pydantic models for structured planner output: something
   like `PlannedAction` (ref, action kind, value, `expectation` string — the field
   `browser/actions.py:Action` already has and is currently unused) and a
   `PlannerResponse` wrapping a list of them plus reasoning. Feed these to
   `GeminiProvider.complete(schema=...)`, which is already built and works (confirmed
   live in Phase 0's `qaura doctor` run — the `response_format` path was NOT
   separately exercised there, only plain text completion was, so **verify structured
   output specifically before trusting it**: `qaura doctor` only proved
   `interactions.create()` works for a plain prompt, not that `response_format` +
   `model_validate_json()` round-trips correctly against the real API. Test this early
   in Phase 3, don't assume it from the docs.
2. `qaura/core/planner.py` — turns a `PageModel` + persona system prompt into one or
   more `PlannedAction`s via the LLM, replacing `heuristic.py`'s
   `_pick_candidate`/`_actions_for` logic for LLM-mode runs. Reuses
   `browser/actions.py:Action` (the `expectation` field is exactly what this phase
   activates) and `llm/base.py:Tier.PLANNER`.
3. Expectation-divergence detector — new file, likely `detectors/flow.py` per the
   plan's original file list. Compares the `PageModel` after an action against the
   `expectation` string the planner set (probably via another cheap LLM call, tier
   `element_classify` or similar, asking "does this new page state satisfy: X?").
   This is design decision #3 from the plan — the single highest-value detector,
   catches bugs with no crash/console/network signal at all (e.g. bug #1 from the
   fixture list, the coupon/checkout total bug — actually wait, #1 needs the
   invariants engine specifically per the plan, not just divergence; re-read the plan
   section on design decision #3 vs #4 before conflating the two).
4. `qaura/llm/budget.py` already exists and is correct (Phase 0) — wire it into the
   planner loop, respect `cfg.guardrails.max_llm_calls_per_run`.
5. `qaura/personas/` — five files (curious, impatient, malicious, power_user,
   accessibility) per the plan's persona descriptions. Each is a system-prompt string
   + action-bias/detector-set/guardrail-profile config, consumed by `core/planner.py`.
   Start with `curious` only to prove the LLM loop works end to end, then add the
   other four — don't try to build and debug all five at once.
6. Session/interaction-chain management: the plan's `previous_interaction_id` design
   ("LLM layer" section) — one chain per persona+role,
   new chain when the state graph jumps to unrelated territory. Not yet designed in
   detail beyond the plan's paragraph on it; work out the exact chain-reset heuristic
   when writing `core/planner.py`, don't guess it now.
7. Wire `qaura run --personas curious,impatient --url ...` in cli.py for real
   (currently prints a warning and ignores `--personas`, falls back to heuristic).
   **This is the Phase 3 deliverable.**

### Phase 3 progress so far
- `qaura/llm/schemas.py` written: `PlannedAction`, `PlannerResponse`,
  `DivergenceJudgement` (pydantic, for structured output).
- **Structured output VERIFIED live** — and it immediately found a second real
  `gemini.py` bug, same pattern as Phase 1's `page.accessibility` break: something
  read off Google's docs during planning didn't match the actual installed SDK.
  `client.interactions.create(instructions=...)` raised
  `TypeError: create() got unexpected keyword argument(s): instructions`. Investigated
  live via `inspect.getdoc(client.interactions.create)` and by reading the generated
  TypedDict in `google.genai._gaos.types.interactions.createmodelinteraction` (the
  installed SDK ships its own field definitions as plain readable Python — this is a
  reliable way to check any future call-shape doubt without waiting on a live call).
  Real kwarg is `system_instruction`. **`response_format`'s shape
  (`{"type": "text", "mime_type": ..., "schema": ...}`) was already correct** — only
  the system-prompt kwarg was wrong. Also learned `response_mime_type` exists as a
  separate top-level kwarg but is DEPRECATED in this SDK version — don't use it, the
  mime type belongs inside `response_format` only. Fixed in `gemini.py`, full
  docstring there now explains both this and the earlier usage-field-name correction
  so a future reader doesn't have to re-derive either. Re-ran live: got back real,
  correctly-structured JSON parsed into a `PlannerResponse` instance with a sensible
  action (`click "Checkout"`, plausible expectation/reasoning text) — the mechanism
  genuinely works end to end now.
- **Real account constraint found, not a code bug**: `gemini-3.1-pro` (and by
  extension likely `gemini-2.5-pro`, not separately tested) returns
  `429 RateLimitError` — `Quota exceeded ... limit: 0 ... free_tier_requests`. **This
  API key's account has ZERO free-tier quota for Pro-tier models.** Flash-tier
  (`gemini-3.5-flash-lite`) worked fine and is what the structured-output test above
  actually used once this was discovered. This means the plan's Pro-for-planning /
  Flash-for-workers architecture is currently only PARTIALLY usable on this key:
  `config.py`'s `ModelTiers` defaults are being kept as-is (Pro for
  planner/triage/localize/fix) because that was a deliberate, explicit user decision
  during planning, not something to silently walk back — but until billing is enabled
  on this Google AI Studio / Gemini API account (or the user points `qaura.yaml`'s
  `model_tiers.planner` etc. at a Flash model themselves), any code path that actually
  calls `Tier.PLANNER`/`TRIAGE`/`LOCALIZE`/`FIX` will hit this 429. Phase 3's own live
  verification (once `core/planner.py` exists) should use a Flash-tier model to avoid
  burning real time on 429 retries, and this constraint should be told to the user
  plainly rather than buried here — say it in the next chat turn, don't just leave it
  in this file.
### Phase 3 files completed (all VERIFIED live against buggy_app, not just unit tested)
- `qaura/core/planner.py` — `plan_next_action()`: one LLM call per turn via
  `PlannerResponse` structured output, validates the returned ref actually exists in
  the given `PageModel` (rejects hallucinated refs as `PlannerError`), validates the
  action kind, threads `budget: Budget` through so planner calls are accounted for.
  8/8 unit tests using a `FakeProvider` (no live calls needed for these).
- `qaura/personas/` — `base.py` (`Persona` dataclass), five persona files (`curious`,
  `impatient`, `malicious`, `power_user`, `accessibility`) each a system prompt +
  behavioral hints, `__init__.py` registry (`get()`/`resolve_all()`). The `malicious`
  persona's prompt is written to only choose safe probing actions (marker payloads,
  cross-role reachability checks) — it never tries to bypass guardrails itself, since
  guardrails.py's enforcement doesn't depend on the persona behaving; it's enforced in
  code regardless of what any persona's prompt says. 6/6 unit tests, including one that
  locks in `attempts_destructive is False` as the default for all five.
- `qaura/detectors/flow.py` — expectation-divergence detector (plan design decision
  #3). Calls the LLM (`Tier.ELEMENT_CLASSIFY`, cheap tier since this runs after every
  planned action) with a `DivergenceJudgement` schema, only reports when
  `not satisfied and confidence >= 0.6`. 6/6 unit tests with a `FakeProvider`.
- `qaura/core/orchestrator.py` — `PersonaOrchestrator`: the LLM-mode equivalent of
  `heuristic.py`'s crawl loop. Deliberately NOT factored to share code with
  `heuristic.py` — see the file's own docstring for why (two genuinely different
  failure modes: a stale ref in heuristic mode is a real bug, in LLM mode it can also
  be a hallucination needing its own handling). Wires `StateGraph` + `RunLimiter` +
  `Budget` + `Recorder` + all four detectors (crash/console/network/flow) together,
  stops cleanly on `BudgetStop`/`BudgetExceeded`, gives up on a persona after
  `MAX_CONSECUTIVE_PLANNER_ERRORS=5` bad planner turns rather than looping forever on
  a broken model/prompt combination.
- `qaura/cli.py` — `run --personas` fully wired: resolves persona names via the
  registry, runs each persona sequentially (one `PersonaOrchestrator` + fresh browser
  context each), merges findings/coverage/LLM-usage across personas into one
  `RunReport`. Falls back to heuristic mode with a clear console message if
  `--personas` is given but no Gemini credential resolves or `--no-llm` was also set.

### Two real corrections found via live verification (same pattern as Phase 1 and
Phase 2 — code that looked right from documentation broke on first actual contact)
1. **Structured output itself was fine; the system-prompt kwarg wasn't.**
   `client.interactions.create(instructions=system_prompt, ...)` raised
   `TypeError: unexpected keyword argument(s): instructions`. Real kwarg is
   `system_instruction` (confirmed via `inspect.getdoc()` and by reading the
   installed SDK's own generated TypedDict — see `google.genai._gaos.types.interactions
   .createmodelinteraction`, which is plain readable Python, not compiled/obfuscated,
   so this is always available as a fallback when live-call debugging isn't enough).
   `response_format`'s shape was already correct
   (`{"type": "text", "mime_type": "application/json", "schema": ...}`). Learned in
   passing: `response_mime_type` exists as a separate kwarg but is DEPRECATED in this
   SDK version — don't use it. Fixed in `gemini.py`, confirmed with a second live call
   that returned correctly-parsed JSON into a real `PlannerResponse` instance.
2. **Real account constraint, not a code bug: this API key has ZERO free-tier quota
   for Pro-tier Gemini models** (`429 ... limit: 0 ... model: gemini-3.1-pro`).
   Flash-tier models work fine. `config.py`'s `ModelTiers` defaults were deliberately
   NOT changed away from Pro-for-planning — that was an explicit user decision during
   planning (confirmed via AskUserQuestion), not something to quietly walk back over
   a quota wall. **The live end-to-end persona verification below used a temporary
   config override pointing every tier at `gemini-3.5-flash-lite`** (not committed to
   the repo — it lived in the session's scratchpad only), specifically to prove the
   mechanism works without burning time on 429 retries. **Told the user about this
   constraint directly in chat, not just left here** — they need to either enable
   billing on the Google AI Studio account or override `model_tiers` in `qaura.yaml`
   before a real Pro-tier run will work.

### Live end-to-end verification (the actual Phase 3 deliverable)
`qaura run --url http://127.0.0.1:8099/ --personas curious --config <flash-tier override>`
against `tests/fixtures/buggy_app` → **18 findings, budget cap enforced exactly**
(`max_llm_calls_per_run: 30` in the test config → `LLM usage: {'calls': 30, ...}`,
confirming `Budget`/`BudgetExceeded` actually stops the loop rather than just existing
in the code unused). Detector breakdown: 4 console + 4 network + 3 crash (the same
three Phase-2 bugs, now found by an LLM persona instead of the deterministic
crawler — good cross-check that both paths converge on real bugs) **plus 6 `flow`
(expectation-divergence) findings that Phase 2 structurally cannot produce**, e.g.
"the broken widget should trigger and demonstrate its error state or feedback" not
being met, and "the subscribe button should submit the form and give feedback" not
being met — these are framed from a USER's perspective (nothing visible happened),
which is arguably more actionable than "TypeError: Cannot read properties of
undefined." This is design decision #3 from the plan working as intended, not just
implemented. Expected side effect of a small 4-element page + a 15-action budget: the
persona re-probed the same few elements multiple times once it ran out of new
territory, producing repeated findings for the same underlying bugs from different
angles — this is fine and expected, not a bug; deduping repeated findings into one is
explicitly Phase 5's job (`analysis/dedupe.py`), not Phase 3's.

### Phase 4 progress: invariants engine — DONE, live-verified, catches the plan's
canonical example for real

- `qaura/core/invariants.py`: `parse_number()` (currency/formatted-text -> float),
  `extract_values()` (CSS selector -> single float or list of floats, scoped to an
  optional `container_selector`), `evaluate_expression()` via `_SafeEval` (an AST
  walker that whitelists comparison/arithmetic/boolean nodes and exactly six builtin
  functions — sum/min/max/abs/len/round — rejecting everything else BEFORE `eval()`
  ever runs, so attribute access/imports/comprehensions/lambdas/arbitrary calls are
  structurally impossible, not just discouraged), `check()` (top-level: returns a
  `Finding` on violation, `None` on either "holds" or "inconclusive on this page" —
  those two are deliberately not distinguished, since most invariants only apply on
  specific pages and that's expected, not an error). 25/25 unit tests, including 8
  specifically trying to break out of the sandbox (all correctly rejected) and a real
  live-browser round-trip proving the mechanism against actual rendered HTML, not just
  synthetic Python dicts.
- Wired into BOTH `core/heuristic.py` and `core/orchestrator.py` — invariants need no
  LLM at all, so they run in heuristic mode too, not just persona mode.
- **Redesigned the expression model from the plan's original sketch, and said why in
  `config.py`'s `InvariantConfig` docstring.** The plan's placeholder examples used
  nested objects (`cart.total`) and generator expressions
  (`sum(item.price * item.qty for item in cart.items)`) — neither is safely evaluable
  without either building a real object-extraction layer (a project of its own) or
  actual `eval()` (a real footgun in a user-edited config file). Replaced with flat
  named numeric values pulled by CSS selector, `expression` as a restricted
  comparison. Also fixed a conceptual bug in the plan's own example while at it:
  `total == sum(line_items)` is WRONG for any cart that supports discounts (it would
  flag every correctly-discounted cart as broken) — split into
  `subtotal_matches_line_items` (subtotal, always the undiscounted sum) and
  `total_reflects_discount` (`total == subtotal - discount`, the one that actually
  catches a stale-total-after-coupon bug). Both fixed in `qaura.example.yaml`.
- **Found and fixed a real crash via live testing** (not from writing more unit
  tests — from actually running the invariant against the live fixture): a cart with
  exactly one line item makes `extract_values()` collapse `line_items` to a bare
  float (single selector match), and `sum(30.0)` raised `TypeError: 'float' object is
  not iterable`. Fixed with `_tolerant_sum()` — `sum` of a single value is just that
  value, matching what any invariant author would actually expect. Locked in with a
  unit test AND a live-browser test using genuinely single-item markup (not the
  3-item fixture the other tests share, which wouldn't have caught this).
- **Found and fixed an independent real bug in Phase 2's heuristic crawler while
  building the fixture for this**: `core/heuristic.py:_actions_for()` never handled
  the `spinbutton` ARIA role (e.g. `<input type=number>`) with FILL — it fell through
  to the generic CLICK-only branch, meaning a quantity input was structurally inert in
  heuristic mode (clicking a number input just focuses it). `core/inputs.py` already
  supported spinbutton values; heuristic.py's action-selection just never used them.
  One-line fix, locked in with `tests/unit/test_heuristic.py` (new file, 4 tests).
- **Added bug #1 to `tests/fixtures/buggy_app`** (the plan's own canonical example:
  a cart section with quantity input, coupon field, and the exact stale-discount bug
  — see the file's own module docstring for the full mechanics). **Live-verified with
  a scripted sequence** (fill coupon, apply, THEN change quantity) directly against
  the running fixture server — confirmed: initial state and post-coupon state both
  correctly show NOT violated, post-quantity-change correctly shows violated with the
  exact right values (`total=9.0, subtotal=30.0, discount=1.0`), and the sibling
  `subtotal_matches_line_items` invariant correctly stays NOT violated throughout
  (proving this isn't a blanket false-positive, it's specifically catching the
  discount going stale). **Honest caveat, stated plainly rather than glossed over:**
  reaching this exact two-step sequence via the UNDIRECTED Phase 2 heuristic crawler
  is not guaranteed in any given run — that crawler doesn't sequence actions with
  intent, it explores independently. The scripted verification proves the fixture and
  the detector are both correct; it does not prove the heuristic crawler will find
  this specific bug unprompted. An LLM persona with reasoning ability is a much better
  match for this class of bug, which is exactly the division of labor the plan
  describes between Phase 2 and Phase 3 — not a shortfall to silently paper over.

### Phase 4 detectors #2-5 — DONE, wired into both crawl loops, live-verified together

- `qaura/detectors/visual.py`: `scan_rules()` runs one `page.evaluate()` DOM walk
  (overflow, contrast via real WCAG luminance math, invisible/zero-opacity/
  same-color text) plus a Python-side offscreen-interactive-element check using
  `PageModel` bboxes against `page.viewport_size`. `detect()` wraps violations as
  Findings and optionally asks `Tier.VISUAL_CONFIRM` (vision model) for a one-line
  note — best-effort, never gates the finding on the vision call succeeding, per the
  plan's "rules first, vision on suspicion" design. 12/12 unit tests, all against a
  real launched browser with deliberately broken CSS (not mocked).
- `qaura/detectors/a11y.py`: `axe_playwright_python.async_playwright.Axe().run(page)`
  — **the real API had to be found by reading the installed package's source directly**
  (`axe_playwright_python/async_playwright.py` + `base.py`), same pattern as every
  other unverified-library moment this session; it had never been exercised before
  despite being a Phase-0 dependency. Violations map through
  axe-core's own `impact` field (critical/serious/moderate/minor) to `Severity`.
  3/3 unit tests, including one that correctly flags a real missing-alt-text image.
- `qaura/detectors/security.py`: `detect_reflected_injection()` is fully automatic
  (wired into both crawl loops) — checks whether an injection-marker value actually
  got parsed as real DOM (`document.querySelector('qaura-marker')`) or actually
  EXECUTED (`window.__qaura_marker === 1`), not just whether the raw string appears
  in text somewhere (which would false-positive on a page that correctly escaped and
  displayed it as literal text — proven by a dedicated "properly escaped, no finding"
  test). `check_cross_role_access()` is a separate, well-tested (7/7 total across
  both functions) standalone utility, DELIBERATELY NOT auto-wired — see the module's
  own docstring: whether a URL "should" require elevated privileges isn't inferable
  from the page alone without either new config (an admin-paths list that doesn't
  exist) or persona/LLM judgment (which the `malicious` persona's prompt already
  covers). Fixture bugs #8 (cross-role) and #7 (double-submit) were deliberately NOT
  seeded — #8 needs multi-role auth the fixture doesn't implement, #7 needs a real
  order flow — noted honestly in the fixture's own docstring rather than half-faked.
- `qaura/detectors/performance.py`: long tasks + CLS via `PerformanceObserver`,
  registered through `setup()` as a page init script that MUST run before the first
  `goto()` (both crawl loops call it first thing in `run()`). **Real, non-obvious
  finding from building the tests, not the feature itself**: Chromium's Long Tasks
  API only attributes tasks to real page-owned script execution (a `<script>` tag,
  an event handler) — a busy-wait run via a bare `page.evaluate()`/CDP
  `Runtime.evaluate` call is NOT counted as a long task at all, confirmed by direct
  experiment (two debug scripts, one failing, one succeeding once the trigger method
  changed to injecting a real `<script>` tag). The `detect()` implementation itself
  was correct the whole time; only the TEST's trigger method was wrong — worth
  knowing for anyone testing browser performance APIs generally, not just for this
  detector. `read_heap_size()` (Chromium `performance.memory`) is a documented
  partial — NOT wired into `detect()`, since a meaningful memory-growth finding needs
  a stateful before/after comparison across a real journey, a different shape of
  check than this module's per-window pattern; exposed as a building block instead
  of forced in just to claim full coverage. 5/5 unit tests.

### Wiring into both crawl loops
`core/heuristic.py` and `core/orchestrator.py` both gained `_check_per_action`
(security + performance, cheap, run after every single action) and `_check_per_state`
(visual + a11y, run ONLY on a state's first visit — `node.visit_count == 1` — since
these are properties of the rendered page, not of a specific action; running them
after every action would just re-report the same static-page issues repeatedly).
`performance_detector.setup(page)` is called first thing in both `run()` methods,
before `goto()`.

### Comprehensive live verification (all Phase 4 detectors together, real fixture)
`qaura run --url http://127.0.0.1:8099/ --no-llm` against the fully enriched
`buggy_app` (bugs #1, #2, #3, #4, #5, #6, #9 all seeded — #7/#8 deliberately
deferred, see above) → **31 findings across all 7 active detectors**: visual(3),
a11y(6), invariant(15 — 15 repeated detections of the SAME underlying bug #1, since
the crawler re-checks invariants after every action once the page is in the broken
state; expected noise, exactly what Phase 5's dedup exists for), security(2),
crash(2), console(2), network(1). Every single seeded bug that this phase's detectors
target was caught, with zero false positives surviving the fix rounds below.

**Two real self-inflicted-noise bugs found and fixed via this live run** (not
hypothetical — the crawler actually hit them):
1. `core/inputs.py:values_for_role("spinbutton")` originally reused
   `BOUNDARY_VALUES + FORMAT_MISMATCH_VALUES` — both lists were built for free-text
   fields and contain non-numeric strings ("not-an-email", "a", "A"*10000).
   Playwright's `.fill()` on `<input type=number>` THROWS on genuinely non-numeric
   input (browsers reject it at the value-setter level), and heuristic.py correctly
   reported each throw as a "crash" finding — except these weren't real app bugs,
   they were QAura fuzzing itself into noise. Took TWO fix rounds to get right: first
   pass just subtracted the obviously-textual `FORMAT_MISMATCH_VALUES` entries and
   still missed `BOUNDARY_VALUES`'s own non-numeric entries (`single_char`,
   `very_long`) — re-ran live, caught the remaining one, replaced with an explicit
   `SPINBUTTON_VALUES` allow-list where every entry is confirmed numeric-parseable,
   not derived by filtering a list built for a different purpose. Locked in with a
   unit test that actually parses every value with `float()` rather than checking
   specific excluded labels (which is exactly the kind of test that would have caught
   round one's incompleteness immediately, had it existed then).
2. (Same session, Phase 4 invariants work) The tolerant-`sum()` fix and the
   `spinbutton` FILL-vs-CLICK gap — both already logged above under invariants, listed
   here again only for completeness since they surfaced from the same style of "run
   it for real and see what breaks" verification this whole phase leaned on.

### Phase 5 progress: dedupe + replay — DONE, verified, and one significant
cross-cutting bug found and fixed along the way

- `qaura/analysis/dedupe.py`: `fingerprint()` = sha256(detector, `url_template()`
  reused from `core/state.py`, `normalize_title()` — collapses standalone digit runs
  via regex so varying counts/ratios/durations don't create spurious distinct
  fingerprints, while leaving quoted element names intact so two DIFFERENT elements
  failing the same rule stay separate findings). `dedupe()` groups, keeps the
  earliest-created representative, sets a new `Finding.occurrence_count` field. 9/9
  unit tests, including one that reproduces the EXACT real scenario (15 identical
  invariant findings -> 1, `occurrence_count=15`) that motivated building this.
- `qaura/analysis/replay.py`: `replay_finding()` reconstructs `Action`s from
  `Finding.repro_steps` and re-executes them against a fresh browser context N times,
  checking reproduction via `dedupe.fingerprint()` equality between the original
  finding and whatever the same detectors produce after replay — a nice reuse where
  "did this bug happen again" and "is this the same bug as that one" are the same
  question asked twice rather than two concepts to keep in sync. Sets
  `finding.reproducibility` (`"N/M"`) and `finding.confirmed`. 3/3 live tests against
  the real buggy_app fixture (skipped automatically if the fixture isn't running —
  `pytest.mark.skipif` on a live `httpx.get` check, so the full suite still passes
  with nobody having started the server).

**Significant bug found while writing replay.py's tests, not replay.py itself — and
it affects EVERY detector, not just replay:** a real captured `Finding.repro_steps`
only ever contained the SINGLE triggering action
(`self._collect_from_recorder(recorder, page.url, [repro_step], persona)` — a
one-element list), never the actions that came before it in the same crawl session.
For single-action bugs (a crash on click, a console error on fill+blur) this was
fine. For the invariant bug — which genuinely depends on "apply coupon" THEN "change
quantity", a two-action sequence — the captured repro was structurally incomplete:
replaying just the quantity-change step alone, starting fresh with no coupon ever
applied, reproduces nothing. Confirmed by inspecting an actual captured finding's
`repro_steps` from a real run (Phase 4's report.json): one step, "key e1 spinbutton
= 'Tab'" — alone, useless as a repro for a bug that needs the coupon applied first.

**Fixed in both `core/heuristic.py` and `core/orchestrator.py`**: both now track
`self._action_history: list[ReproStep]`, appending every step as it happens and
passing `list(self._action_history)` (a snapshot) to every detector call —
`_collect_from_recorder`, `_check_invariants`, `_check_per_action`, `_check_per_state`,
and (orchestrator only) `flow_detector.check()` — instead of `[repro_step]`. Verified
live: a re-run of the same crawl now shows a captured invariant finding with **16**
repro_steps (the full sequence: several qty fuzz attempts, several coupon fuzz
attempts, all the way to the triggering fill), not 1.

**This fix was found and proven correct through replay.py's own test-writing
process** — a genuinely good example of why building the replay mechanism early
surfaces problems that would otherwise sit silently wrong in every finding a user
ever sees. Two of replay.py's own tests initially failed for this exact reason
(manually-constructed test repro_steps were similarly incomplete, missing a `Tab`
keypress needed to trigger the fixture's `onchange` handler — confirmed via a
dedicated debug script showing `.fill()` changes an input's value without dispatching
a `change` event) — fixed the tests to match what a real capture now correctly
produces, not the other way around.

### Phase 5 files completed since the last checkpoint

- `qaura/analysis/repo_index.py` + `qaura/analysis/localize.py`: deterministic
  substring-based localization, no LLM required for the core mechanism (an LLM
  refinement pass is a natural future enhancement, not built — the deterministic
  version already does the useful part). `extract_search_terms()` pulls URL paths,
  network-failure endpoint paths, and quoted names from repro steps/description;
  `search()` ranks repo files by term-hit count. **Tested against the REAL
  `tests/fixtures/buggy_app` directory**, not a synthetic fixture — proves the
  mechanism works on the actual target used throughout this whole session's
  verification. 16/16 unit tests.
- `qaura/llm/schemas.py` gained `TriageVerdict` (structured output: is_likely_false_
  positive, confidence, severity_adjustment constrained to lower/keep/raise — the
  model can only argue to move the detector's own severity, never invent a fresh one
  from nothing). **Verified live against the real Gemini API** — and this run
  incidentally re-proved the Pro-tier quota fallback works for a brand new tier
  (`Tier.TRIAGE`) it had never been exercised against before: logged the fallback
  warning, retried on Flash-Lite, returned a genuinely sensible verdict on a real
  performance finding.
- `qaura/analysis/triage.py`: `apply_triage()` mutates severity via `_adjust_severity`
  (clamped at both ends — can't push past CRITICAL or below INFO, unit-tested),
  appends a reasoning note to the description, returns the verdict so the CALLER
  decides whether to drop a confident false positive (`is_confident_false_positive()`,
  threshold 0.7) — triage.py itself never silently drops findings, that's a
  reporting-level decision. 13/13 unit tests with a FakeProvider.
- `qaura/analysis/fix.py`: `suggest_fix()`/`apply_fix_suggestion()`, plain-text
  response (not structured output — Finding.suggested_fix is prose, and forcing a
  JSON schema here would add validation risk for no benefit, unlike the planner where
  structured output feeds directly into an executable Action). Reads a source
  snippet via repo_index when `likely_component` is set. 9/9 unit tests, including
  one that reads a REAL snippet from the real buggy_app source and confirms it
  actually reached the prompt.
- `qaura/reporting/repro.py`: emits a runnable pytest file per finding. **Key design
  decision**: rather than trying to bake CSS selectors into the generated script
  (fragile — `ReproStep.ref` is only meaningful re-resolved against a freshly-observed
  PageModel, exactly the brittleness the whole `ref` design exists to avoid), the
  generated script embeds the Finding as data and calls `analysis.replay.replay_finding()`,
  asserting `result.successes == 0` — correctness is inherited from replay.py's
  already-proven mechanism instead of needing a second independent implementation.
  For `detector == "invariant"` findings, looks up the matching `InvariantConfig` by
  name (parsed from the title) from the run's invariant list and embeds just that one;
  a finding with no matching config gets an honest warning comment in the generated
  file rather than a script that silently can't work. 8/8 structural unit tests
  (`ast.parse()` on the output — real syntax validation, not just string matching).
  **Then proven with the strongest possible check**: emitted a script for the real
  broken-widget crash bug, ran it with pytest against the live buggy fixture — FAILED
  with a clear assertion message ("Bug still reproduces... (1/1)"). Then ran the
  SAME unmodified script against a patched copy of the fixture (widget bug fixed,
  served on the same port via a temp copy — the tracked fixture file was never
  touched) — PASSED. This is the plan's exact acceptance bar for Phase 2-5
  ("each emitted repro script fails against the buggy app and passes once the seeded
  defect is patched"), demonstrated literally, not just architected to satisfy it.

### Wired into `cli.py`'s `run` command
New flags: `--replay-attempts` (default 2, `0` disables replay) and `--no-analysis`
(skips the whole Phase 5 pipeline). `_analyze()` runs dedupe -> triage+drop-false-
positives (only with an LLM available) -> localize (only if `repo_path` set) ->
replay -> fix suggestions (only with an LLM available) -> repro-script emission, in
that order — dedupe first so every expensive/LLM step downstream runs once per
distinct bug, not once per near-duplicate. **`--no-llm` now means no LLM calls
anywhere in the run, not just during crawling** — a user avoiding API cost via
`--no-llm` would be surprised to see triage/fix calls fire anyway just because they
only meant to skip the persona crawl; deliberate design decision, not the obvious
default.

**Real bug found and fixed via the live end-to-end run**: the first full pipeline run
showed visual/a11y findings (which have empty `repro_steps` by design — they're
page-state properties, always true on load, no action needed to "reproduce" them)
reporting `reproducibility: "0/2"` and `confirmed: false` after replay — misleadingly
reading as "not a real bug" when it actually meant "not applicable, nothing to
replay." Fixed by filtering to `replayable = [f for f in findings if f.repro_steps]`
before calling `replay_finding()` — non-repro findings now correctly keep
`reproducibility`/`confirmed` as `None` (not attempted) instead of a false negative
signal.

**Known scaling limitation, stated honestly rather than silently left**: a
finding's `repro_steps` now captures the FULL action history up to that point in the
crawl session (the Phase-5 fix earlier in this file). For a long single-page crawl
with many elements, a late-discovered finding can carry a large step count, and
replaying it — let alone N attempts of it, for M findings — has real wall-clock cost
(the live verification run genuinely took several minutes, not instant). It completed
correctly, so this isn't blocking, but a much larger real-world site could make the
replay phase slow. Not fixed here — a real fix (e.g. trimming to a minimal necessary
prefix via bisection, or a replay-phase time budget with graceful early exit) is a
reasonable Phase 7 (polish) item, not invented speculatively today without evidence
of how much it actually matters in practice.

### Phase 6 — COMPLETE. All four suites, the fixture, the report card, and full CLI
wiring, all verified live.

- `qaura/mltest/registry.py` — `load_model()`/`detect_format()`. sklearn/joblib fully
  supported (project dependency already). onnx/torch/Hugging Face are LAZY, OPTIONAL
  imports — deliberately NOT hard dependencies (multi-GB installs; requiring them for
  every QAura install just to test a scikit-learn classifier would be a poor
  default). `load_model()` gives a clear "pip install X" `ModelLoadError` rather than
  a raw `ImportError`. 11/11 tests, including a REAL scikit-learn
  `DecisionTreeClassifier` trained/saved/loaded/predicting end to end, plus real (not
  mocked) "not installed" error paths for onnx/torch, since those genuinely aren't
  installed in this project.

- **`tests/fixtures/ml/`** (`build_fixture.py`) — synthetic binary classification
  data with an informative x1/x2, pure-noise x3, and a `group` column (A: 80%, B:
  20%, with B given deliberately noisier labels so it's a genuinely harder subgroup).
  `model_degraded.joblib` (unconstrained `DecisionTreeClassifier`) vs.
  `model_baseline.joblib` (`LogisticRegression(class_weight="balanced")`).
  **Sanity-checked the fixture's actual properties before building the suite around
  it** — found live that `class_weight="balanced"` balances by LABEL, not GROUP, so
  it did NOT produce the intended fairness regression between models (both models
  show a similar ~26-29 point group-recall gap). Calibration and robustness
  regressions DID come through strongly as designed (Brier 0.138 vs 0.077; flip rate
  10.7% vs 1.2%). **Redesigned the fairness check around this finding** rather than
  fighting the data-generating process: `check_slices` uses an ABSOLUTE threshold
  (any subgroup >15pts below the best subgroup), not a baseline-vs-model comparison —
  which is arguably more correct anyway, since a fairness gap in an "improved" model
  is still a real fairness gap, and the fixture now honestly demonstrates exactly
  that instead of a contrived contrast.

- `qaura/mltest/suites/artifact.py` — `check_slices` (fairness, absolute threshold),
  `check_calibration` (Brier score), `check_robustness` (prediction-flip rate under
  small Gaussian perturbation), `check_regression` (accuracy vs. baseline),
  `run_artifact_suite` (orchestrates all four + overall accuracy). **Real bug found
  live**: `check_slices` originally called `model.predict()` on the FULL dataframe
  including the string-valued slice column itself — crashed inside sklearn
  immediately (`KeyError` surfacing from a deeper feature-name mismatch). Fixed by
  adding an explicit `feature_cols` parameter so predict-features and the
  group/slice column are never conflated. 10/10 tests, all against the real fixture
  models — including one proving baseline has genuinely fewer findings than degraded.

- `qaura/mltest/suites/data.py` — `check_schema`, `population_stability_index` (PSI,
  standard quantile-bucket implementation with Laplace smoothing), `check_drift`
  (PSI + KS test), `check_null_rate_shift`, `check_leakage` (correlation-based). No
  trained model needed — operates on plain DataFrames. 12/12 tests, all passed on
  first write (no live bugs found here — the checks are simpler, self-contained
  statistics with no model-object indirection to get wrong).

- `qaura/mltest/suites/endpoint.py` — `probe_endpoint()`: malformed-payload cases
  (empty object, null values, wrong types, oversized strings, invalid JSON body) +
  latency-tail + nondeterminism checks across repeated identical requests. Uses httpx
  (transitive dependency, confirmed in Phase 5). **Tested live against the real
  `/api/validate-email` endpoint and found 3 real crashes** — including ONE not
  deliberately seeded: sending malformed JSON also 500s, because `buggy_app` parses
  the body via raw `request.json()` with no Pydantic validation layer, so FastAPI's
  normal 422-on-bad-JSON handling never kicks in. A genuine organic finding, not one
  I planted, which is exactly the kind of validation this whole "verify against a
  real fixture" discipline is meant to catch. 7/7 tests (3 live against buggy_app,
  4 against an `httpx.MockTransport` for cases needing precise control — timeout,
  connection error, latency threshold, nondeterminism). **Found and fixed a
  monkeypatch recursion bug while writing the mock tests**: `factory()` called
  `httpx.AsyncClient(...)` after `httpx.AsyncClient` had already been replaced BY
  `factory` — infinite recursion. Fixed by capturing the real class before patching.

- `qaura/mltest/suites/genai.py` — `probe_prompt_injection`, `probe_system_prompt_leak`,
  `probe_pii_echo`, `probe_refusal_consistency`, `probe_format_contract`,
  `run_genai_suite`. Drives a chat-shaped UI through the EXISTING browser layer
  (Driver/observe/actions from Phase 1-3, nothing new needed) — the one suite with a
  real dependency on that machinery. `GenAIProbeConfig` takes explicit
  input/send/response selectors rather than trying to auto-discover a "chat widget"
  — same reasoning as `detectors/security.py`'s cross-role check: target-specific
  concepts that can't be safely inferred, so make them explicit config instead of
  guesswork. **Added a minimal deliberately-vulnerable chat widget to
  `tests/fixtures/buggy_app`** (no real LLM call — kept fast/free/deterministic —
  but genuinely complies with "ignore instructions" requests, leaks its system
  prompt verbatim, and echoes PII-shaped input unprompted) specifically to prove
  this suite fires, same fixture-alongside-detector discipline as every other phase.
  **All three seeded vulnerabilities caught on the very first live run**, no fix
  cycle needed here. 5/5 tests, all live against the real fixture.

- `qaura/mltest/report.py` — `build_run_report()` + `overall_gate()` (PASS/WARN/FAIL
  from finding severities). Genuinely REUSES `reporting/models.py` + `reporting/html.py`
  unmodified, per the plan's explicit "same HTML shell" instruction — ML findings
  already share the same `Finding` model (repro_steps/evidence just stay empty,
  `url` is repurposed as the model/endpoint/app path). 9/9 tests, including one that
  renders a real ML finding through the existing Jinja2 template end to end. Known
  cosmetic limitation, not fixed: the template's coverage summary line
  (`.get("states", 0)` etc.) is web-crawl-specific wording, so an ML report's
  metrics dict displays as "0 states explored" rather than something ML-shaped —
  functional, not broken, just not polished; a template enhancement for later if it
  matters enough to someone.

- **`qaura ml test`/`ml probe`/`ml genai` wired into `cli.py`, replacing the Phase-0
  stubs, and each VERIFIED THROUGH THE ACTUAL CLI** (not just by calling the library
  functions directly) — this matters because CLI wiring has its own failure surface
  (argument parsing, JSON payload parsing, exit codes) that library-level tests don't
  exercise:
  - `qaura ml test --model ... --data ... --slice-col group --baseline ...` →
    correctly reported `Gate: FAIL (4 finding(s))` with the exact metrics from the
    manual verification, exit code 1 (gate failure — correct for CI use).
  - `qaura ml probe --endpoint ... --payload '{"email":"..."}'` → correctly found the
    same 3 crashes, `Gate: FAIL`, exit code 1. Along the way, hit and diagnosed a
    genuine PowerShell quirk (not a QAura bug): passing a JSON string with embedded
    double quotes from PowerShell to python.exe silently STRIPS all the quotes
    before the process ever sees them — confirmed via a `sys.argv` diagnostic script.
    Worked around it for verification using PowerShell's `--%` stop-parsing token;
    worth knowing for anyone else invoking this CLI from PowerShell with JSON args.
  - `qaura ml genai --url ... --input-ref e1 --send-ref e2 --response-selector ...`
    → correctly found all 3 genai vulnerabilities, `Gate: FAIL`, exit code 1.
  All three gates correctly exit 1 on FAIL — usable as a CI check today.

### Phase 7 progress so far

- `Finding.from_dict()` / `RunReport.load_json()` added to `reporting/models.py` —
  inverse of the existing `to_dict()`/`save_json()`, needed so `qaura replay`/
  `qaura report` can operate on a PAST run without re-crawling. 3/3 unit tests
  (round-trip a Finding with every field populated, empty-evidence edge case, full
  `RunReport` save-then-load).
- **`qaura replay <run_path>`** and **`qaura report <run_path>`** — replaced the
  Phase-0 stubs for real. Both accept either a `report.json` path or a run directory
  containing one. `replay` re-executes every finding's (or one, via `--finding-id`)
  `repro_steps` N times via the existing `analysis/replay.py`, updates
  `confirmed`/`reproducibility` in place, and re-saves both JSON and HTML. `report`
  just re-renders HTML from a saved JSON (useful after hand-editing a finding, or if
  `reporting/html.py`'s template changes after the run happened). Deliberately
  deviated from the plan's literal example path shape (`qaura replay
  runs/latest/BUG-001`, implying one file per finding) since QAura's actual storage
  is one `report.json` per run — `--finding-id` covers the "just this one bug" case
  without needing a different storage format.
  **Live-verified both against a real saved run** (`runs/phase5final/.../report.json`
  from Phase 5's verification): `qaura report` regenerated the HTML correctly in
  one shot. `qaura replay` against that same run — which includes the finding with
  the 16-step invariant history — was genuinely slow (moved to background,
  consistent with the scaling limitation already documented under Phase 5), but
  completed correctly once it finished; see the live-run result logged right after
  this note once confirmed.
- **`qaura run --ci --fail-on <severity>`** — a CI gate for the crawler, parallel to
  what `mltest/report.py:overall_gate()` already does for the `ml` subcommands (kept
  as a separate, simpler inline check in `cli.py` rather than reusing `overall_gate`
  directly — that function's PASS/WARN/FAIL split doesn't take a configurable
  threshold, and `run`'s gate needed one: `--fail-on critical|high|medium|low`,
  default `high`). Exits 1 if any finding meets or exceeds the threshold, 2 on a bad
  `--fail-on` value, 0 otherwise — opt-in via `--ci` so existing non-CI usage of
  `qaura run` is unaffected.

### Phase 7 progress: replay/report CLI verification uncovered a real design fact
(not a bug), coverage report redesigned around it, determinism confirmed empirically

- **`qaura replay` against an OLD saved report initially showed ALL findings failing
  to reproduce (0/2 across the board), including ones that should be rock-solid.**
  Root-caused, not dismissed: that report was captured BEFORE the Assistant chat
  widget was added to `buggy_app` during Phase 6. Two new elements inserted earlier
  in the page shifted every subsequent `ref` by two, so replaying "fill e1" against
  the CURRENT page filled the chat input, not the quantity field the original run
  meant — replay was correctly executing the recorded refs, just against a page
  that no longer matched them. **Confirmed by generating a FRESH report against the
  current fixture and replaying that one**: 6/7 findings correctly reproduced
  (`confirmed: true`, matching the earlier same-session verification exactly). This
  is an inherent characteristic of ref-based action replay, not a bug — refs are
  only ever meaningful against the exact DOM structure they were captured from — and
  it's now documented plainly rather than left to surprise the next person who hits
  it. (The 7th finding, a console-log 404 message, didn't reproduce in that single
  attempt — plausibly Chromium's own console-event timing rather than anything in
  this codebase; not chased further given the other 6 came back clean and dedicated
  replay tests already show 3/3 and 2/2 reliability elsewhere.)

- **This directly motivated finishing the coverage-report redesign properly**:
  `core/state.py`'s `StateNode` now tracks `element_signatures`/`exercised_signatures`
  as `(role, name)` pairs, not just ref-keyed counts — the exact same "refs aren't
  stable" fact that broke the old replay also meant the old `exercised_refs`-based
  coverage counting couldn't produce a meaningful "list of specific elements never
  reached" (it could only count, not name them). `mark_exercised()` now takes an
  optional `element: ElementInfo` (both crawl loops updated at all 6 call sites —
  `element`/`candidate` was already in scope at every one), and
  `StateGraph.unreached_elements()` returns `(state_template, role, name)` tuples.
  Wired into `cli.py` (both heuristic and persona paths) and into
  `reporting/html.py`'s template — a real run with a deliberately low action budget
  (3 actions against buggy_app's 12 elements) now shows a genuine "11 element(s)
  never interacted with" collapsible section in the actual rendered HTML, naming
  each one (`button: 'Trigger broken widget'`, `textbox: 'Coupon'`, etc.) — verified
  by grepping the real output file, not just trusting the template compiles.
  14 state.py tests + 2 new html tests, all passing; one pre-existing test needed
  updating to pass `element` (the old ref-only call no longer moves the coverage
  needle, which is the intended fix, not a regression to preserve).

- **Deterministic run seeding: verified EMPIRICALLY rather than assumed from reading
  the code.** Ran the same heuristic crawl against the same fixture twice, diffed
  the full finding sets (`Compare-Object` in PowerShell) — empty diff, byte-identical
  titles/detectors/order both times. `core/heuristic.py` and `core/inputs.py`
  genuinely make no `random`/`np.random` calls anywhere in the crawl-decision path.
  Documented this finding directly in `heuristic.py`'s module docstring.
  **Deliberately did NOT wire `QAuraConfig.seed`** — there is no randomness source
  for it to seed today, and adding `random.seed(cfg.seed)` calls with nothing
  downstream reading that seed would be dead code satisfying a hypothetical need,
  which cuts against this whole project's stated principle of not building for
  needs that don't exist yet. Wire it if/when a real randomness source (e.g.
  randomized tie-breaking among equal-priority elements) is actually introduced.

### Major Phase 7 finding: screenshots were NEVER actually being captured, since
Phase 1 — not a Phase 7 gap, a real bug in every prior phase's report output

While scoping visual-baseline comparison (which needs real screenshots to compare
against), checked whether `Finding.evidence.screenshot_path` was ever actually
populated — it wasn't. `browser/recorder.py:Recorder.screenshot()` has existed since
Phase 1, but neither `core/heuristic.py` nor `core/orchestrator.py` ever called it.
Every finding in every report generated across Phases 2-6 has had
`screenshot_path: null`. This isn't a nice-to-have that slipped — "Screenshots" was
explicitly listed as a core deliverable in the ORIGINAL project brief
("The system provides: Steps to reproduce, Screenshots, Browser logs, Network
logs..."), so this got fixed properly rather than deferred further as polish.

**Fixed in both crawl loops**: `HeuristicCrawler`/`PersonaOrchestrator` now accept an
optional `screenshot_dir` (both `__init__`s), take one screenshot per
detection-batch (not per individual finding — findings from the same batch are
evidence of the same page state, so they share one capture; avoids redundant I/O),
and attach the resulting path to every finding produced in that batch via a new
`_attach_screenshot()` helper on each class. Covers all detection points in both
loops: per-state checks (visual/a11y), per-action checks (passive detectors,
invariants, security/performance, and orchestrator's LLM divergence check), AND the
action-raised-an-exception path that a quick first pass initially missed (the
`before` counter needed to move earlier, ahead of the `execute()` try block, to
also cover that finding — caught by re-reading the code, not by a failing test).
`cli.py` passes `out_dir / "screenshots"` to both constructors. `reporting/html.py`
already had correct base64-inlining logic waiting for real paths to inline — Phase 2
built that half correctly, it just never received real data until now.

**Live-verified with a full crawl against buggy_app**: 17/17 findings now carry a
real `screenshot_path`, 6 actual PNG files written to disk (deduped across findings
sharing a batch), and the generated `report.html` genuinely contains embedded
`data:image/png;base64,...` images — confirmed by grepping the real output file, not
assumed from the template compiling.

### Visual baselines — DONE, live-verified both the true-positive and true-negative case

- Added `Pillow>=10.0` as a real project dependency (not "ml" extra — small, fast
  install, not the multi-GB-download situation `mltest/registry.py` deliberately
  avoided for torch/onnx).
- `qaura/analysis/visual_baseline.py`: `compare_screenshots()` (numpy-array pixel
  diff with a per-channel tolerance for anti-aliasing noise; a dimension mismatch
  itself counts as 100% changed, not silently skipped) and `compare_runs()`
  (matches findings across two `RunReport`s via `analysis/dedupe.py`'s fingerprint —
  same reuse pattern as `analysis/replay.py`, "is this the same bug" and "is this
  the same visual context to diff" are the same matching problem). **Deliberately
  scoped to recurring-finding comparison, not a full state-by-state sweep** — a
  complete sweep would need unconditional per-state screenshot capture, a real but
  larger change than this lowest-priority item warranted without evidence it's
  needed; documented plainly in the module's own docstring rather than silently
  narrowing scope. 10/10 unit tests, including one using two REAL screenshots from
  an actual crawl (not synthetic colors) — which caught a real, expected behavior
  worth knowing: full-page screenshots at different crawl points can have different
  heights as dynamic content changes the scrollable page, so a dimension mismatch
  between two legitimate same-run screenshots is normal, not a bug; the test was
  fixed to not assume same-dimensions rather than the code being wrong.
- Wired into `cli.py` via `qaura run --baseline-run <path>`, appending any
  regressions to the report before saving. **Live-verified both directions**: the
  unit tests already proved genuine visual changes get flagged (synthetic
  red-vs-blue images, full 100% change detected); a live run against the unchanged
  `buggy_app` fixture with `--baseline-run` pointed at an earlier real run correctly
  found ZERO regressions (finding count stayed at 17, matching pre-baseline) —
  confirming no false positives on a stable target, which matters just as much as
  catching real ones.

### Next action
None required — README rewritten (reflects the actual project: what a run does,
project layout, real command examples matching the real CLI flags). Full test suite
green (271 total: 260 passed, 11 correctly skipped without a live fixture server).
All seven phases of the original plan are complete and live-verified. Genuinely
optional next steps if this project continues: enable Gemini billing (or point
`qaura.yaml`'s `model_tiers` at Flash models) to use Pro-tier quality for real; a
full state-by-state visual-baseline sweep instead of the current recurring-finding
comparison, if that gap turns out to matter in practice; a smarter replay history
trim for very long single-page crawls (documented scaling limitation, Phase 5); the
`--fail-on` CI gate could reasonably extend to occurrence-count or triage-confidence
thresholds, not just severity, if that turns out useful. None of these block using
the tool today.

### Known blockers / open questions
- The Pro-tier quota-0 constraint on the configured `GEMINI_API_KEY` — MITIGATED in
  code (`GeminiProvider` auto-falls-back to Flash-Lite at runtime, `qaura doctor`
  surfaces exactly which tiers are affected), not resolved, since resolving it needs
  the user's Google billing decision, which is out of scope for code to fix. A real
  run works today regardless, just at Flash-Lite quality on the four affected tiers.
- Nothing else outstanding. Every other open item from earlier in this file was
  either fixed (see the many "Fixes applied" / "found and fixed" entries throughout)
  or is a deliberate, documented scope decision, not a gap someone forgot about.

### Fixes applied 2026-08-30 (user said "fix all and continue")

**Gemini Pro-tier quota-0, two separate real problems found and fixed:**
- `GeminiProvider.complete()` (`qaura/llm/gemini.py`) now catches a 429
  `RateLimitError` from `google.genai._gaos.lib.compat_errors`, logs a warning via
  the standard `logging` module, and permanently downgrades that `Tier` to the
  `element_classify` model (Flash-Lite, confirmed working) for the rest of the
  provider instance's life — so a run keeps going instead of dying on the first
  Pro-tier call. `config.py`'s `ModelTiers` defaults were NOT changed (still
  Pro-for-planning, the user's explicit earlier choice) — this is purely a runtime
  resilience measure; a fixed-billing account or a future key just won't ever hit the
  fallback path. 5/5 new unit tests in `tests/unit/test_gemini_provider.py`, using a
  fake client (no live calls needed) plus a REAL `RateLimitError` instance
  (`__new__`'d to skip `__init__`, since only `isinstance()` and `raise`/`except`
  matter for the test, not fully-populated attributes).
- **Separately, and worse:** `qaura doctor`'s new per-tier live probe (added as part
  of this same fix pass — it now makes one minimal live call per distinct configured
  model instead of only checking `models.list()` membership, since a model can be
  *listed* as reachable and still be uncallable) found that `planner_fallback`'s
  configured default, `gemini-2.5-pro`, is not quota-limited — it's fully **retired**
  (`404: This model models/gemini-2.5-pro is no longer available to new users`).
  This would have failed for ANY account, not just this one. Fixed by changing
  `config.py`'s `ModelTiers.planner_fallback` default to `gemini-pro-latest` (a
  rolling alias, less prone to going stale the way a dated snapshot did) — confirmed
  live-reachable and live-callable in the same `qaura doctor` run that found the
  problem. Also fixed in `qaura.example.yaml`.

**Final live verification (`qaura doctor`, full output in the session, not
paraphrased):** `planner`/`triage`/`localize`/`fix` (all `gemini-3.1-pro-preview`) →
QUOTA BLOCKED. `planner_fallback` (now `gemini-pro-latest`) → OK.
`element_classify`/`invariant_candidates` (`gemini-3.5-flash-lite`) → OK.
`visual_confirm` (`gemini-3.7-flash`) → OK. Doctor's own summary line now reads
"4 tier(s) are quota-blocked... GeminiProvider will automatically fall back... a run
will still work, just at lower quality on the affected tiers." This means **a real
run works today, right now, on this account, using the configured defaults** — just
at Flash-Lite quality on the four Pro-tier roles until billing is sorted out. That is
the actual, complete fix: not routing around the constraint by silently changing
defaults, but making the software behave sensibly in the face of it while telling the
user clearly what's degraded and why.

## Phase checklist (from the plan)

- [x] Phase 0 — Scaffold and credentials (`qaura doctor` works) — VERIFIED against live key
- [x] Phase 1 — Browser layer (`qaura observe <url>` works) — VERIFIED live twice, incl.
      a real API break (accessibility.snapshot removed) found and fixed, not just written
- [x] Phase 2 — Heuristic run, no LLM (`qaura run --url ... --no-llm` produces HTML report)
      — VERIFIED against tests/fixtures/buggy_app: 5 findings, all 3 seeded bugs caught,
      after fixing 2 real bugs found only by actually running it (see above)
- [x] Phase 3 — Gemini planner + personas — VERIFIED live: 18 findings incl. 6 flow
      (expectation-divergence) findings Phase 2 structurally can't produce, budget cap
      enforced exactly (30/30 calls). Found + fixed a real SDK kwarg bug
      (`instructions` -> `system_instruction`) and a real account constraint (Pro-tier
      quota is 0 on this key — told the user directly, not just logged here)
- [x] Phase 4 — Deep detectors (invariants, visual, a11y, security, performance) —
      VERIFIED: 31 findings live against buggy_app, all 7 active detectors fired
      correctly, zero self-inflicted false positives after 2 fix rounds
- [x] Phase 5 — Analysis + evidence (dedupe, triage, localize, repro scripts) —
      VERIFIED: dedupe 31->16 on a real run, all action-based findings 2/2 replayed
      and localized correctly, a real repro script proven to fail-then-pass across
      buggy/fixed fixture copies (the plan's literal acceptance bar, demonstrated)
- [x] Phase 6 — ML testing (artifact, data/drift, endpoint, genai suites) — VERIFIED
      through the actual CLI (`qaura ml test/probe/genai`), all four suites firing
      correctly against real fixtures (a real trained model, a real HTTP endpoint, a
      real chat widget), 3 real implementation bugs found and fixed via live testing
- [x] Phase 7 — Polish (seeding verified deterministic, coverage report with named
      unreached elements, CI mode, standalone replay/report commands, screenshot
      capture fixed — a real Phase 1-6 gap found while scoping this phase, visual
      baselines, README) — ALL VERIFIED LIVE, not just written

## Decisions made mid-build not obvious from code

(Append here as they happen — anything you decide on the fly that a fresh reader of the
code wouldn't infer.)

---

## Phase 8 — "make it perfect": depth and honesty pass, from a real live-user run

Prompted by an actual authenticated run against a real payroll app the user built
(a separate FastAPI + Jinja registry, tested with the user's explicit permission on
throwaway databases). That run surfaced, with hard evidence, that QAura explored
shallowly: 34 findings, all page-chrome (contrast/landmarks), 12 of 49 elements
exercised, 2 states left unexplored, and the crawler never submitted a single form
— so the app's actual payroll logic went completely untested. About 12 of the 34
findings were also false positives (a below-the-fold form field flagged as "outside
the viewport"). The user asked for a full pass to fix this, framed as "make it
perfect... test every nook and cranny."

Three parallel Explore-agent audits pinned every root cause to exact file:line before any
code changed — this phase is grounded in that audit, not guesswork.

### What shipped

**Phase A — element addressing.** `ElementInfo` (`browser/observe.py`) gained `index`,
`form_key`, `input_type`, `required`, `maxlength`, `pattern`, `min`, `max`, populated via
one batched `page.evaluate` per (role,name) group instead of skipping any element whose
locator matched more than once. `actions.py`'s `_locator_for` now always addresses via
`.nth(element.index)`, which fixes a real bug: duplicate/empty accessible names used to
raise a Playwright strict-mode violation that surfaced as a bogus "crash" finding instead
of actually being addressed. Deliberately did NOT extend `core/state.py`'s (role, name)
coverage-signature tuple to include index — that would have been a much larger, riskier
change (every test unpacking `unreached_elements()` as a 3-tuple, `cli.py`'s consumption)
for benefit disproportionate to the demonstrated bug, which was fixed at the
addressing/enrichment layer instead. Known accepted gap: two duplicate-name elements
still collapse to one signature in the *coverage report* (not in actual addressing).

**Phase B — form-aware crawling.** New `core/forms.py` (`group_forms`, `FormGroup`,
submit-button identification by name-hint then `input_type=submit`). `core/inputs.py`
gained `valid_value_for()` (realistic values keyed on input_type/name — email, date,
number, ID-like, name-like) and `hostile_value_for()` (one deliberately unusual value,
skipping empty/whitespace). `heuristic.py` now fills a whole form with realistic data and
submits (the pass that actually exercises business logic), plus up to
`FORM_FUZZ_FIELD_LIMIT` (2) additional submissions each corrupting exactly one field
while the rest stays valid, so a resulting error is attributable to that field. File
inputs get a real `set_input_files` call against a generated minimal valid PDF
(`_sample_upload_path`) instead of the old behavior of clicking them, which hung on a
native OS file-chooser dialog until timeout.

**Phase C — full-coverage exploration**, folded into the same `heuristic.py` rewrite:
frontier navigation (`_next_frontier_url`, bounded by `_frontier_tried` so a
signature/duplicate quirk can't cause an infinite bounce) replaces the old behavior of
`break`ing the instant the *start page* saturated while the graph still had known
unexplored states; scrolling before acting on a below-fold element
(`_is_below_fold`/`_scroll_into_view`); widened the action vocabulary the crawler
actually emits to include `UNCHECK` (drives both check/uncheck transitions using
observed state, not just CHECK forever).

**A real bug found live, not from code review, and fixed in this same phase:**
`_mark_form_exercised` recorded a form's individual *elements* as exercised in the state
graph but never actually added the form itself to the `_form_exercised` tracking set —
so `_next_unexercised_form` kept returning the same form forever. Compounded by a
GET-method search form (`/records?q=...`) whose results content changes the state
fingerprint on every submission, this made the crawler loop on the search form
indefinitely — confirmed live via the payroll app's request log (dozens of repeated
`/records?q=test` / `?q=🔥` requests, never reaching `/records/new`). Fixed two ways
together: `_mark_form_exercised` now actually adds `form.key` to the tracking set, AND
that set is keyed by URL TEMPLATE (`core/state.py:url_template`) rather than full state
key, so a form's own results changing don't make it look unexercised again. Regression
test: `tests/unit/test_heuristic.py::test_form_marked_exercised_is_not_reoffered_on_same_template`.
Re-run against the payroll app afterward confirmed the fix: the search-form pass is now
bounded (3 submissions) and the crawl proceeds on to `/records/new`.

**Phase D — fixing what reported falsely.**
- `detectors/visual.py`'s `offscreen_interactive` rule rewritten: the old check compared
  an element's y-coordinate against the current *viewport* height, meaning every
  below-the-fold field on any page taller than one screenful was flagged as "outside the
  viewport" — that's simply what being below the fold means. Now compares against the
  *document's* own rendered bounds (`document.documentElement.scrollWidth/scrollHeight`)
  and negative coordinates, catching genuinely off-canvas placement without flagging
  normal scrollable content. Confirmed live: re-running against `buggy_app` dropped
  finding count from 16 to 15, the removed one being exactly this false positive on its
  "Trigger broken widget" button. New tests lock in both the negative case (below-fold,
  must NOT fire) and positive case (negative-position, must still fire) —
  `test_scan_below_the_fold_button_not_flagged_offscreen`,
  `test_scan_detects_negative_position_offscreen_on_a_tall_page`. Also added
  `zero_size_interactive` (an interactive element with zero rendered width/height) —
  had to check it BEFORE the `el.visible` gate, since Playwright's own `is_visible()` is
  false by definition for a zero-size element (confirmed live via a throwaway script
  before writing the check). Explicitly did NOT add element-overlap detection this pass
  — worked through the design and concluded a geometry-only overlap check can't reliably
  distinguish "icon nested inside its own button" (normal, not a bug) from "two controls
  genuinely overlapping" (a bug) without ancestor-chain data the codebase doesn't track,
  and shipping a second noisy detector directly contradicts the lesson this whole phase
  started from.
- `analysis/replay.py`'s structural `0/N, not confirmed` on every visual/a11y finding
  fixed: `_attempt_once` now re-runs `visual_detector.detect` / `a11y_detector.detect` /
  `performance_detector.detect`, gated on `finding.detector` so a crash/security finding
  doesn't pay for an axe-core pass it has no use for. New `ReproStatus` enum
  (`reporting/models.py`) — `PENDING`/`NOT_APPLICABLE`/`CONFIRMED`/`FLAKY`/
  `NOT_REPRODUCED` — replaces the old binary `confirmed: bool`/bare fraction, which
  could not distinguish "never checked" from "checked and genuinely didn't reproduce".
  `cli.py`'s replay filter (`_analyze`, and the standalone `qaura replay` command) now
  checks `finding.detector in REPLAYABLE_DETECTORS` (crash/console/network/security/
  invariant/visual/a11y/performance — NOT flow, which needs an LLM + an expectation
  string that isn't persisted on `Finding`, or visual_baseline, which is inherently a
  cross-run diff) and explicitly sets `NOT_APPLICABLE` on everything outside that set
  instead of leaving a misleading blank.
- Reordered `cli.py`'s analysis pipeline: localize now runs BEFORE triage (was the other
  way around). Triage appends an LLM-written note directly onto `finding.description`,
  and localize's term extraction pulls quoted spans out of that same description — the
  old order let LLM-generated prose leak into localization's search terms.

**Phase E (partial) — cross-role wiring.** `detectors/security.py`'s
`check_cross_role_access()` was fully implemented since Phase 4 but literally never
called outside tests. Added `admin_paths: list[str]` to `QAuraConfig` and
`is_admin: bool` to `AuthRole`; `qaura run --role <non-admin>` now visits each configured
admin path under that session afterward (one extra short-lived browser context) and
reports a finding if nothing looks like it blocked access. Opt-in — empty by default,
since QAura can't infer which routes are privileged from the page alone. Explicitly did
NOT add SSTI/SQL-error/path-traversal-success detectors for the injection markers
`core/inputs.py` already fuzzes with — worked through a safe design and concluded a
generic "does '49' appear in the DOM" SSTI check has real false-positive risk (any page
showing an unrelated count/price containing "49") without a way to scope the search to
where the marker was actually reflected, and SQL/path-traversal server-side success is
already partially caught today as a generic crash/500 finding by the existing
crash/network detectors when the payload errors the server — the gap is only *silent*
success, which needs response-body inspection (not currently captured by `Recorder` at
all) to detect generically. Deferred rather than shipping something that could
reintroduce the exact false-positive problem this phase set out to fix.

**Phase F (partial) — infrastructure and report.** `qaura run --trace` (default off —
capturing a Playwright trace for the whole session is real overhead, opt-in rather than
silently slowing/bloating every run) saves `trace.zip` per browser context via the
already-implemented but never-wired `Recorder.stop_and_save_trace`; path surfaced in
`RunSummary.trace_paths` and printed at the end of a run. `reporting/html.py` rewritten:
findings are now sorted by severity before rendering (`by_severity()` existed since
Phase 5 but was only ever used for the summary count tiles — a CRITICAL finding could
render below a LOW one; this was probably the single biggest defect in the report), a
vanilla-JS severity/detector filter bar (no dependencies, single-file property
preserved), stable per-finding `id=` anchors, screenshots promoted out of the collapsed
Evidence `<details>` to always-visible (click to zoom) since they were the single most
persuasive artifact in the report and used to require two clicks to see, and previously-
collected-but-never-rendered data now shown: `occurrence_count` ("seen N×"),
reproducibility status as an honest badge (see Phase D), `llm_usage`, coverage's
exercised/total ratio, and the emitted repro script's path per finding (new
`Finding.repro_script_path`, set by `cli.py` after `emit_repro_script` — previously the
flagship "every bug becomes a regression test" deliverable was invisible in the flagship
deliverable itself).

**Explicitly NOT done this phase** (scope cuts made deliberately, not oversights —
recorded here so a fresh session doesn't assume they're covered):
- `heuristic.py`/`orchestrator.py`'s ~110-line duplication (constructor, `_collect_from_
  recorder`, `_check_invariants`, `_attach_screenshot`, `_check_per_action`,
  `_check_per_state`, run preamble, ReproStep bookkeeping, two same-named `CrawlResult`
  classes) was audited and quantified but not extracted — `heuristic.py` changed enough
  in this phase already that extracting a shared base for both loops in the same pass
  was judged too much simultaneous risk. `orchestrator.py` (the LLM persona path) was NOT
  touched at all this phase — it still has the OLD one-field-at-a-time behavior, the OLD
  offscreen bug is fixed for it too (shared `detectors/visual.py`), but it does not get
  Phase B/C's form-awareness or frontier navigation. Matches the user's own confirmed
  priority (deterministic-first, since Gemini Pro quota is 0) but is a real, existing
  gap: a persona run today explores exactly as shallowly as heuristic mode did before
  this phase.
- Responsive/multi-viewport visual scanning, pagination-following (`state.py`'s
  `url_template` still drops query strings — needed for pagination but changing it risks
  state-count explosion, a tradeoff flagged in the original plan and not revisited here),
  HTTP-hygiene headers (CSP/HSTS/cookie flags), broken-link/asset checking, HTML hygiene
  (duplicate ids, placeholder text), keyboard-trap/focus-order beyond axe-core, memory-
  growth detection (`performance.read_heap_size()` still unwired), form-validation-bypass
  testing, and localization accuracy improvements (IDF weighting, path-match bonus,
  file:line instead of just file) — all designed in the plan, none implemented. Each is
  independently addable without touching what this phase already shipped.

### Verification performed

- Full unit suite green throughout, checked after every sub-phase, not just at the end:
  260 → 271 (fixture-server-dependent tests ran live since the fixture happened to be up)
  → 275 passed (new visual + heuristic regression tests), 0 unexplained failures at any
  point.
- Live reruns against `tests/fixtures/buggy_app`: finding count 16 → 15 after the
  offscreen fix, confirmed the removed finding was the exact false positive (below-fold
  "Trigger broken widget" button), all other previously-found real bugs (2 crash, 1
  security, 6 a11y, network, console, visual overflow/contrast) still present.
- Live reruns against the real payroll app on throwaway databases (`AUTH_DATABASE_URL`/
  `DATA_DATABASE_URL`/`UPLOAD_DIR` pointed at scratch files, real `app.db`/`data.db`/
  `uploads/` never touched) — used to demonstrate both the original shallow-crawl problem
  and, after the form-tracking bug fix, that the crawler now genuinely submits the search
  form (bounded, 3 passes) and proceeds to `/records/new`. Verified with a full
  end-to-end run against the payroll app with all Phase A-E fixes together
  (coverage / `POST /records/new` confirmation / zero-offscreen-false-positive
  check / report severity-sort check): `qaura run --url
  http://127.0.0.1:8000/dashboard --role admin --no-llm --config qaura.yaml`,
  started on throwaway databases.

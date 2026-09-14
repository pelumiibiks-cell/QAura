# QAura command reference

Every `qaura` command available in this project, with every flag, pulled directly from `qaura/cli.py`. All commands run through the venv, from the `QAura` project directory:

```
.\.venv\Scripts\python.exe -m qaura.cli <command> [options]
```

(If `qaura` is on your PATH after `pip install -e .`, you can drop the `.\.venv\Scripts\python.exe -m qaura.cli` prefix and just type `qaura <command>`.)

For what each command actually *does* under the hood, see `HOW_IT_WORKS.md`. This file is the flag-by-flag reference.

## Global options

These go before the command name, e.g. `qaura --verbose run --url ...`.

| Flag | Meaning |
|---|---|
| `--verbose`, `-v` | Show debug-level diagnostics on stderr, including errors that are normally handled silently (a failed locator lookup, a detector that couldn't run) |
| `--version` | Print the installed version and exit |

---

## `qaura doctor`

Validates the Gemini credential, lists reachable models, and makes one live call per configured model tier to check both reachability and quota. Run this first — everything LLM-related assumes it passed. Safe to run with no key at all (heuristic mode doesn't need one).

| Flag | Default | Meaning |
|---|---|---|
| `--config`, `-c` | auto-detect `qaura.yaml` | Path to a specific config file |

```
qaura doctor
qaura doctor --config qaura.yaml
```

---

## `qaura init`

Analyzes a website and writes a proposed config for it, so you don't have to hand-write `qaura.yaml`. Runs a bounded, strictly read-only crawl, infers guardrails/personas/admin paths from what it sees, synthesizes business-rule invariants and validates them against captured page snapshots, then writes an annotated proposal to `qaura.generated.yaml`.

**It never writes `qaura.yaml`.** Review the output and copy it over yourself, or run against it directly with `--config`.

Every value in the generated file says how it was inferred and how confident that inference is. Anything low-confidence is written commented out.

| Flag | Default | Meaning |
|---|---|---|
| `--url` | required | Seed URL to analyze |
| `--role` | none | Analyze through a captured auth session. Exits 1 if no session exists for that role (unlike `observe`/`run`, which warn and continue anonymously) |
| `--out` | `qaura.generated.yaml` | Where to write the proposal |
| `--max-pages` | `25` | Hard cap on pages visited |
| `--max-depth` | `2` | Link depth from the seed URL |
| `--no-llm` | off | Skip invariant synthesis. Guardrails, personas and admin paths are still inferred, so this is useful with no API key at all |
| `--interact-safe` | off | Also click tabs and disclosure toggles to reveal hidden content. Never submits a form |
| `--login-form` | off | On a login wall, try `QAURA_LOGIN_USER`/`QAURA_LOGIN_PASS`. Plain credential forms only |
| `--check` | off | Don't generate. Report whether `--config`'s invariant selectors still match the live site |
| `--ignore-robots` | off | Crawl paths `robots.txt` disallows. For infrastructure you own |
| `--yes` | off | Skip the interactive authorization prompt (CI). You are still asserting you're authorized |
| `--force` | off | Overwrite an existing `--out` file |
| `--headless` / `--headed` | `--headless` | Show the browser window or not |
| `--config`, `-c` | auto-detect | Existing config, read for the Gemini credential and model tiers. Never written to |

```
qaura init --url http://localhost:8000
qaura init --url http://localhost:8000 --no-llm          # no API key needed
qaura init --url http://localhost:8000 --role admin      # analyze authenticated routes
qaura init --url http://localhost:8000 --check -c qaura.yaml   # are my selectors still good?
```

**Exit codes:** `0` success · `1` output exists without `--force`, or a missing role session · `2` authorization declined or no terminal to ask on · `3` bot protection detected · `4` the generated config failed its own validation (a bug — please report it).

### Read-only guarantee

Recon issues only GET and HEAD requests, enforced at the request layer rather than by policy: a route handler on the browser context aborts every non-GET request, including ones the target's own JavaScript fires on load. No forms are submitted, and destructive-looking paths (`/logout`, `/delete`, `/checkout`) are blocked even for GETs, since plenty of apps still ship GET-triggered logout links.

The single exception is `--login-form`, which permits exactly one same-origin POST, armed immediately before the login submit and disarmed immediately after.

Pointing this at a host you don't operate is unauthorized testing. Remote targets require you to type the hostname to confirm.

### What it can and can't infer

**Reliably:** `target_url`, `allowed_domains`, `blocked_paths`, rate and budget caps, `personas.enabled`.

**Only when measured:** `admin_paths`. With `--role admin` it visits each discovered path anonymously and records which ones are actually refused. Without that it falls back to guessing from path names, which is written commented out — a wrong entry here makes the cross-role check report a HIGH-severity finding against a route that was never privileged.

**Not at all:**

1. **Login.** Recon detects a wall and tells you to run `qaura auth capture`, but it can't log itself in. `--login-form` covers plain credential forms; MFA, SSO and CAPTCHA don't work and won't.
2. **Selector stability.** Generated selectors are only as good as the site's markup. On a site with `data-testid` attributes this is solid. On Tailwind-JIT or CSS-modules class hashes it falls back to structural paths that break on the next redesign. Each generated invariant is labelled `durable`, `moderate` or `fragile` so you can see which is which, and `--check` tells you when one has stopped matching.
3. **Client-side-only routes.** A route that exists only in a compiled bundle, with no link and no sitemap entry, is invisible. SPAs are systematically under-crawled; `--interact-safe` helps by observing `history.pushState`.
4. **`repo_path`.** A website can't tell you where its source lives.
5. **Bot-protected sites.** Recon detects Cloudflare/Akamai/DataDome/Imperva challenges and stops with exit 3 rather than describing the challenge page. It does not try to evade them.

### Reproducibility

The generated file's header carries a `recon_digest` — a hash of what the crawl actually saw. Two runs with the same digest observed the same site, so any difference between their configs came from the LLM rather than from the crawl drifting. Everything except invariant synthesis is deterministic given the same observations.

---

## `qaura observe <url>`

Prints the distilled `PageModel` for a URL — every interactive element with its role, accessible name, value, and state. This is what the crawler/planner actually sees, not raw HTML. Useful for sanity-checking a page, or for finding an element's `ref` (e.g. `e3`) before using `qaura ml genai`.

| Argument/Flag | Default | Meaning |
|---|---|---|
| `url` (positional) | required | URL to load and describe |
| `--role` | none | Load a captured auth session by role first (see `qaura auth capture`) — without this, you see the page as an anonymous visitor |
| `--headless` / `--headed` | `--headless` | Show the browser window or not |

```
qaura observe http://localhost:8000
qaura observe http://localhost:8000/dashboard --role admin
qaura observe http://localhost:8000/login --headed
```

---

## `qaura auth capture`

Opens a **visible** browser at the given URL, waits for you to log in by hand and press Enter in the terminal, then saves the session (cookies/storage) to `.qaura/auth/<role>.json` for reuse by every other command's `--role` flag. Always headed — the whole point is a human completing whatever login flow the app requires (including MFA), which nothing here tries to script.

| Flag | Default | Meaning |
|---|---|---|
| `--url` | required | Where to open the browser (typically the login page) |
| `--role` | `user` | Name this session will be saved and referenced under |

```
qaura auth capture --url http://localhost:8000/login --role user
qaura auth capture --url http://localhost:8000/login --role admin
```

## `qaura auth list`

Lists every captured session (i.e. every `<role>` you can pass to `--role` elsewhere).

```
qaura auth list
```

---

## `qaura run`

The main command — drives a real browser through the target app, finds problems, and writes a report. With no Gemini key (or `--no-llm`), runs the deterministic heuristic crawler at zero API cost; with `--personas` and a working key, runs one or more LLM-driven personas instead.

| Flag | Default | Meaning |
|---|---|---|
| `--url` | `target_url` from config | Target to test. Required if not set in `qaura.yaml` |
| `--no-llm` | off | Force heuristic mode even if a Gemini key is configured |
| `--personas` | none | Comma-separated persona list: `curious,impatient,malicious,power_user,accessibility` — requires a working Gemini key |
| `--role` | none | Use a captured auth session — without this the crawl is anonymous, which on a login-gated app means it only ever sees the login/register pages |
| `--out` | `runs/` (or `output_dir` from config) | Output directory; a timestamped subfolder is created inside it |
| `--config`, `-c` | auto-detect `qaura.yaml` | Path to a specific config file |
| `--headless` / `--headed` | `--headless` | Show the browser or not |
| `--replay-attempts` | `2` | Times to re-run each finding's repro to confirm it's real; `0` disables replay entirely |
| `--no-analysis` | off | Skip dedupe/triage/replay/localize/fix/repro-script generation — just the raw crawl |
| `--ci` | off | Exit code 1 if any finding meets `--fail-on` severity — for CI pipelines |
| `--fail-on` | `high` | Minimum severity that trips `--ci`: `critical`\|`high`\|`medium`\|`low` |
| `--baseline-run` | none | Path to a prior run's `report.json` — flags recurring findings whose screenshot changed meaningfully since then |
| `--trace` | off | Save a Playwright `trace.zip` per browser context — the best debugging artifact available, but slower and produces a larger output, so it's opt-in |

```
qaura run --url http://localhost:8000 --no-llm
qaura run --url http://localhost:8000 --role admin --no-llm
qaura run --url http://localhost:8000 --personas curious,impatient,accessibility
qaura run --url http://localhost:8000 --no-llm --ci --fail-on high
qaura run --url http://localhost:8000 --no-llm --no-analysis
qaura run --url http://localhost:8000 --no-llm --baseline-run runs/20260830_120000/report.json
qaura run --url http://localhost:8000 --no-llm --trace
```

**What determines LLM vs heuristic mode:** `--no-llm` always forces heuristic. Otherwise, `--personas` triggers LLM mode only if `qaura doctor` would succeed (a working `GEMINI_API_KEY` and `llm_mode` in config allows it — `auto` is the default and uses Gemini iff a key is present). If you pass `--personas` without a working key, it prints a warning and falls back to heuristic automatically rather than failing.

**Cross-role checking** runs automatically at the end of a `--role`'d run if `admin_paths` is set in `qaura.yaml` and the role used isn't marked `is_admin: true` — no separate flag needed, see `HOW_IT_WORKS.md`.

---

## `qaura replay <run_path>`

Re-executes one or all findings from a saved run against a fresh browser, confirming (or refuting) reproducibility. Updates `report.json`/`report.html` in place with the new results.

| Argument/Flag | Default | Meaning |
|---|---|---|
| `run_path` (positional) | required | Path to a run directory, or directly to its `report.json` |
| `--finding-id` | none | Replay only this one finding; default replays every eligible one |
| `--attempts` | `3` | Times to re-run each finding |
| `--role` | none | Use a captured auth session — needed if the original run was authenticated |
| `--headless` / `--headed` | `--headless` | Show the browser or not |
| `--config`, `-c` | auto-detect `qaura.yaml` | Path to a specific config file |

```
qaura replay runs/20260830_120000
qaura replay runs/20260830_120000 --role admin
qaura replay runs/20260830_120000 --finding-id BUG-1a2b3c4d --attempts 5
```

Only findings whose detector supports replay get re-checked (crash, console, network, security, invariant, visual, a11y, performance) — others are left alone since re-running them wouldn't mean anything.

---

## `qaura report <run_path>`

Regenerates just the HTML report from a saved `report.json` — useful after hand-editing a finding, or if the report template has changed since the run happened. Doesn't touch the browser at all.

| Argument/Flag | Default | Meaning |
|---|---|---|
| `run_path` (positional) | required | Path to a run directory, or directly to its `report.json` |
| `--out` | next to the input JSON | Output HTML path |

```
qaura report runs/20260830_120000
qaura report runs/20260830_120000/report.json --out my_report.html
```

---

## `qaura ml test`

Runs the model-artifact test suite against a trained model: metrics, per-slice/subgroup performance, perturbation robustness, calibration, fairness gaps, and regression against a baseline. Exits 1 if the overall gate is FAIL.

| Flag | Default | Meaning |
|---|---|---|
| `--model` | required | Path to the trained model file |
| `--data` | required | Path to a CSV with feature columns + a label column |
| `--label-col` | `label` | Name of the label column |
| `--feature-cols` | all columns except label/slice | Comma-separated feature column names |
| `--slice-col` | none | Column to check subgroup fairness on, e.g. `group` |
| `--baseline` | none | Path to a baseline model to check regression against |
| `--out` | `runs/` | Output directory |

```
qaura ml test --model model.joblib --data eval.csv
qaura ml test --model model.joblib --data eval.csv --slice-col group --baseline prior_model.joblib
```

## `qaura ml data`

Compares a reference CSV (usually training data) against a current one (serving or eval data): schema changes, feature drift, and null-rate shift per column. With `--label-col` it also scans `current` for features suspiciously correlated with the label, which usually means leakage. Exits 1 if the gate is FAIL.

| Flag | Default | Meaning |
|---|---|---|
| `--reference` | required | Path to the reference CSV |
| `--current` | required | Path to the CSV to compare against it |
| `--columns` | columns common to both files | Comma-separated numeric columns to check for drift and null-rate shift |
| `--label-col` | none | Also run the leakage check against `current`, using this column as the label |
| `--feature-cols` | all columns except the label | Comma-separated; only used with `--label-col` |
| `--out` | `runs/` | Output directory |

```
qaura ml data --reference train.csv --current serving_sample.csv
qaura ml data --reference train.csv --current eval.csv --label-col label
```

## `qaura ml probe`

Probes a live inference endpoint with adversarial, malformed, empty, oversized, and wrong-type payloads — checks for crashes, schema violations, latency, and nondeterminism. Exits 1 if the gate is FAIL.

| Flag | Default | Meaning |
|---|---|---|
| `--endpoint` | required | URL of the inference endpoint |
| `--payload` | `{}` | JSON of a known-good request payload, used as the baseline to mutate |
| `--method` | `POST` | HTTP method |
| `--out` | `runs/` | Output directory |

```
qaura ml probe --endpoint http://localhost:8000/predict --payload '{"x": 1, "y": 2}'
```

## `qaura ml genai`

Drives a chat-shaped AI feature in a target app through the browser, probing prompt injection, jailbreak, PII echo, and output-contract breakage. Exits 1 if the gate is FAIL.

| Flag | Default | Meaning |
|---|---|---|
| `--url` | required | Page containing the chat feature |
| `--input-ref` | required | `PageModel` ref of the chat input box (find it via `qaura observe`) |
| `--send-ref` | required | `PageModel` ref of the send button |
| `--response-selector` | required | CSS selector for where the response text appears |
| `--refusal-probe` | none | A request that *should* be refused, to check refusal consistency |
| `--role` | none | Use a captured auth session, for a chat feature behind a login |
| `--headless` / `--headed` | `--headless` | Show the browser or not |
| `--out` | `runs/` | Output directory |

```
qaura observe http://localhost:8000/assistant
# find the input/send refs from the output above, then:
qaura ml genai --url http://localhost:8000/assistant --input-ref e1 --send-ref e2 --response-selector "#chat-response"
```

---

## Typical first-time workflow

```
qaura doctor                                              # confirm the Gemini key works (optional — heuristic mode needs no key)
qaura auth capture --url http://localhost:8000/login --role user   # if the app needs login
qaura init --url http://localhost:8000/dashboard --role user       # propose a config; review qaura.generated.yaml
qaura observe http://localhost:8000/dashboard --role user          # sanity-check the authenticated view
qaura run --url http://localhost:8000/dashboard --role user --no-llm   # the actual test run
qaura report runs/<timestamp>                             # regenerate the HTML if you ever need to
```

`qaura init` is optional — you can hand-write `qaura.yaml` instead — but it's the fastest way to get a config with working invariants for an app you haven't tested before. Capture the auth session *before* running it, so it can see the authenticated surface.

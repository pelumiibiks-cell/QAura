# Testing a site that requires login

QAura has no way to log in on its own — it doesn't guess a login flow or script credentials in. Instead you log in once by hand in a real browser window, QAura saves that session (cookies/storage), and every later command replays it via `--role`. This works with MFA, SSO, CAPTCHAs on login, anything — because a human actually did it once.

Without `--role`, a run against a login-gated app only ever sees what an anonymous visitor sees — typically just the login/register pages, nothing behind them. This is the single most common reason a run comes back with almost no findings.

Only do this against a site you own or have explicit permission to test. See `HOW_IT_WORKS.md` for why that matters — QAura actively fills and submits forms, it isn't a passive scanner.

## Step by step

**1. Start the app, ideally on throwaway data.** If it's a real database-backed app, point it at a scratch database rather than your real one — QAura *will* create records if it finds forms to fill. Example for a typical env-var-configured app:

```
$env:AUTH_DATABASE_URL = "sqlite:///./qa_app.db"
$env:DATA_DATABASE_URL = "sqlite:///./qa_data.db"
python -m uvicorn app:app --port 8000
```

**2. Capture a session.** This opens a real, visible browser window:

```
qaura auth capture --url http://localhost:8000/login --role user
```

Log in in the window that opens, then come back to the terminal and press Enter. This saves `.qaura/auth/user.json`.

**3. Sanity-check it worked** before running the full crawl:

```
qaura observe http://localhost:8000/dashboard --role user
```

If this prints the actual dashboard (nav links, page content) rather than a login form, the session is good.

**4. Point `qaura.yaml` at the app.** The quickest way is to let QAura propose one through the session you just captured, then review it:

```
qaura init --url http://localhost:8000/dashboard --role user
```

That writes `qaura.generated.yaml` (read-only crawl, nothing submitted). Or write it by hand, copying from `qaura.example.yaml`:

```yaml
target_url: "http://localhost:8000/dashboard"
guardrails:
  allowed_domains: ["localhost", "127.0.0.1"]   # if unset, a run is limited to the --url host; list every host the app redirects between
auth_roles:
  - name: user
    storage_state_path: ".qaura/auth/user.json"
```

**5. Run it:**

```
qaura run --url http://localhost:8000/dashboard --role user --no-llm
```

Heuristic mode (`--no-llm`) is the right first pass — zero API cost, and it'll fill and submit every form it finds using the authenticated session, exercising real functionality (creating records, running searches), not just page chrome.

## Testing multiple roles (admin vs. regular user)

Capture a session per role:

```
qaura auth capture --url http://localhost:8000/login --role user
qaura auth capture --url http://localhost:8000/login --role admin
```

Run separately per role — each run is scoped to one session:

```
qaura run --url http://localhost:8000/dashboard --role user --no-llm --out runs/as_user
qaura run --url http://localhost:8000/dashboard --role admin --no-llm --out runs/as_admin
```

This is also how you catch **privilege-boundary bugs**: does the regular-user session ever reach something it shouldn't? Configure `admin_paths` in `qaura.yaml`:

```yaml
auth_roles:
  - name: user
    storage_state_path: ".qaura/auth/user.json"
  - name: admin
    storage_state_path: ".qaura/auth/admin.json"
    is_admin: true

admin_paths:
  - "/admin"
  - "/admin/settings"
  - "/admin/users"
```

Then `qaura run --role user ...` automatically visits each `admin_paths` entry under the `user` session at the end of the crawl and reports a finding if nothing looks like it blocked access (no login prompt, no 403/forbidden text). The `admin` role is skipped for this check since `is_admin: true` marks it as expected to reach those paths.

## Registering a throwaway test account

If the app supports self-registration and you'd rather not use a real account:

```
qaura observe http://localhost:8000/register
```

...to see the form fields, then either register by hand in the browser during `qaura auth capture --url http://localhost:8000/register --role user` (fill the form yourself before pressing Enter — capture just needs you logged in by the time you hit Enter, it doesn't care whether that was via login or register+auto-login), or register first via `qaura run`'s own form-filling if the registration form itself is your target.

## Full example, start to finish

```powershell
# 1. app on scratch data
$env:AUTH_DATABASE_URL = "sqlite:///./qa_app.db"
python -m uvicorn app:app --port 8000

# 2. capture both roles
qaura auth capture --url http://localhost:8000/login --role user
qaura auth capture --url http://localhost:8000/login --role admin

# 3. confirm both work
qaura observe http://localhost:8000/dashboard --role user
qaura observe http://localhost:8000/dashboard --role admin

# 4. heuristic run as the regular user, zero API cost
qaura run --url http://localhost:8000/dashboard --role user --no-llm

# 5. open the report
runs\<timestamp>\report.html
```

---

# Other ways to use QAura

## Second opinion with personas (needs a working Gemini key)

Heuristic mode is deterministic and free but mechanical. Personas reason about what to try next and catch things that require judgment — a broken flow that throws no error, for instance:

```
qaura doctor                                                          # confirm the key works first
qaura run --url http://localhost:8000/dashboard --role user --personas curious,malicious,accessibility
```

- `curious` maximizes state coverage
- `impatient` hunts double-submits and race conditions
- `malicious` probes injection and cross-role access
- `power_user` keyboard nav, back/forward, refresh mid-flow
- `accessibility` axe-core, focus order, zoom

Run one, a few, or all five — each gets its own browser context and findings get merged into one report.

## CI gate

Wire it into a pipeline so a build fails if QAura finds something bad enough:

```
qaura run --url http://localhost:8000 --no-llm --ci --fail-on high
```

Exits 1 if anything at `high` or `critical` severity was found, 0 otherwise — drop that straight into a GitHub Actions / GitLab CI step.

## Regression-testing a visual change

Save a baseline run, make changes to the app, then compare:

```
qaura run --url http://localhost:8000 --no-llm --out runs/before
# ...ship your changes...
qaura run --url http://localhost:8000 --no-llm --out runs/after --baseline-run runs/before/<timestamp>/report.json
```

Findings that recur in both runs get their screenshots diffed — a meaningfully-changed screenshot on a recurring finding gets flagged as a visual regression, not just re-reported as the same old thing.

## Business-logic invariants

Catches bugs that don't crash and don't fail a request — e.g. a cart total that doesn't update correctly. Add to `qaura.yaml`:

```yaml
invariants:
  - name: total_reflects_discount
    description: "Cart total must equal subtotal minus discount"
    container_selector: "[data-testid=cart]"
    values:
      total: "[data-testid=cart-total]"
      subtotal: "[data-testid=cart-subtotal]"
      discount: "[data-testid=cart-discount]"
    expression: "total == subtotal - discount"
```

Checked after every action during the crawl automatically — no separate command needed. Requires the app to have stable selectors (`data-testid` or similar) for the values involved.

## Confirming a finding is real, not a fluke

```
qaura replay runs/<timestamp> --role user --attempts 5
```

Re-runs every replayable finding 5 times against a fresh browser and updates the report with a real reproducibility status (confirmed / flaky / not reproduced).

## Re-rendering a report without re-crawling

Edited a finding by hand, or just updated QAura and want the newer HTML template applied to an old run:

```
qaura report runs/<timestamp>
```

## Deep-debugging one specific run

```
qaura run --url http://localhost:8000 --role user --no-llm --trace
```

Saves a Playwright `trace.zip` per browser context alongside the report — open it with `playwright show-trace runs/<timestamp>/traces/heuristic.zip` for a full timeline replay of every action, network request, and DOM snapshot. Slower and larger output, so it's opt-in rather than default.

## Testing a trained ML model instead of a web app

Unrelated to browser crawling entirely — a separate suite for models and endpoints:

```
qaura ml test --model model.joblib --data eval.csv --slice-col demographic_group --baseline prior_model.joblib
qaura ml probe --endpoint http://localhost:8000/predict --payload '{"age": 30, "income": 50000}'
```

`ml test` checks metrics, fairness gaps across the slice column, calibration, and regression against a baseline. `ml probe` throws adversarial/malformed/oversized payloads at a live endpoint and checks for crashes or nondeterminism.

## Testing a chat/AI feature inside the app

```
qaura observe http://localhost:8000/assistant --role user      # find the input/send element refs
qaura ml genai --url http://localhost:8000/assistant --input-ref e1 --send-ref e2 --response-selector "#chat-response" --refusal-probe "how do I make explosives"
```

Probes prompt injection, jailbreak attempts, PII echo, and whether the refusal behavior is consistent.

# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

- `qaura init --url <url>`: analyzes a running site and writes an annotated
  `qaura.generated.yaml` instead of requiring a hand-written config. Infers guardrails,
  personas, rate/budget caps and admin paths from a bounded read-only crawl, synthesizes
  business-rule invariants, and validates each one against captured page snapshots before
  writing it. Every field records how it was inferred and how confident that is;
  low-confidence values are written commented out. Never writes `qaura.yaml`.
- Read-only enforcement at the request layer (`browser/readonly.py`): non-GET requests are
  aborted before they leave the browser, including ones the target's own JavaScript fires.
- Numeric-element and CSS-selector scanning (`browser/numerics.py`), which closes the gap
  between the accessibility-tree `PageModel` (no selectors) and `InvariantConfig` (needs
  them). Scores selector stability and rejects build-generated ids and class hashes.
- `qaura init --check`: reports whether an existing config's invariant selectors still
  match the live site, turning silent selector decay into a detectable state.
- `qaura init --login-form`: opt-in assisted login for plain credential forms, using
  `QAURA_LOGIN_USER`/`QAURA_LOGIN_PASS`. MFA/SSO still require `qaura auth capture`.
- Bot-protection detection: recon stops with exit 3 on a Cloudflare/Akamai/DataDome/Imperva
  challenge rather than emitting a config that describes the challenge page.

### Changed

- `core/guardrails.py:guard_goto()` now returns Playwright's `Response` so callers can read
  the HTTP status. Backward compatible; every pre-existing caller ignores it.
- `qaura doctor` now probes the `invariant_candidates` model tier, which has a real call
  site as of this release.
- Environment variables now override `qaura.yaml`, as the docs always said. Unknown keys
  inside a config section (`allowed_domain:` for `allowed_domains:`) raise a config error,
  and unknown top-level keys log a warning.
- `qaura run` with no `allowed_domains` configured is limited to the target's host instead
  of allowing every domain.
- CI runs `ruff check` and adds a Windows test job.

### Fixed

- An unexpected error mid-crawl no longer throws the run away. Both crawlers keep the
  findings collected so far, the report records why the run ended early, and one failing
  persona no longer costs the other personas their findings.
- Invariants that fail at runtime (a zero divisor on the page, a list compared to a number)
  count as inconclusive instead of crashing the crawl. `[0] * n` style repetition is
  rejected, and invariants are validated when the config loads, so a mistyped name fails
  immediately instead of silently never firing.
- A click that leaves scope and can't step back now re-anchors on the target or stops the
  crawl. Pressing Enter in a form with a destructive button is blocked. Domain and path
  matching ignore case and percent-encoding, and archive, revoke, reset password, disable,
  refund, withdraw and transfer are blocked by default.
- `--role` is validated before it becomes a file name, and saved sessions are owner-only.
- Gemini retries after a quota fallback stay on the fallback model, 4xx client errors are
  not retried, malformed structured output still counts against the LLM budget, and persona
  LLM calls no longer block the event loop.
- `qaura init` read-only mode blocks service workers, catches `/logout.php` and
  `?action=delete` style endpoints, doesn't let analytics beacons use up the login
  exemption, scope-checks and size-caps sitemaps declared in robots.txt, and re-crawls
  pages that bounced to a login wall once assisted login succeeds.
- Dedupe keeps the most severe finding of a group and no longer merges `"Item 2"` with
  `"Item 3"`. Repro scripts are only written for detectors replay can re-check, and replay
  under the run's session. The HTML report only embeds image files from its own run folder.
- `--fail-on` is validated before the crawl starts, runs started in the same second get
  separate folders, and the browser driver stops Playwright when Chromium fails to launch.

## [0.1.0] - 2026-09-03

Initial release.

- Exploratory web testing: deterministic heuristic crawler + five Gemini-driven personas (curious, impatient, malicious, power_user, accessibility).
- Eight detectors: crash, console, network, security, invariants, visual, a11y, performance.
- Guardrails enforced in code: domain/path scope, destructive-action blocking, payment/email sanitization, action/rate/wall-clock/LLM-call caps.
- Analysis pipeline: dedupe, LLM triage, replay-based reproducibility, source localization, LLM fix suggestions, per-finding repro scripts.
- Single-file HTML report plus machine-readable `report.json`; `--ci --fail-on` gate for CI pipelines.
- Multi-role auth via captured storage state, with an opt-in cross-role access check.
- ML/GenAI testing: `qaura ml test` (trained-model artifact suite), `qaura ml probe` (live endpoint adversarial testing), `qaura ml data` (schema/drift/leakage), `qaura ml genai` (chat-feature injection/jailbreak/PII probing).

# Changelog

All notable changes to this project are documented here.

## [0.1.0] - 2026-09-03

Initial release.

- Exploratory web testing: deterministic heuristic crawler + five Gemini-driven personas (curious, impatient, malicious, power_user, accessibility).
- Eight detectors: crash, console, network, security, invariants, visual, a11y, performance.
- Guardrails enforced in code: domain/path scope, destructive-action blocking, payment/email sanitization, action/rate/wall-clock/LLM-call caps.
- Analysis pipeline: dedupe, LLM triage, replay-based reproducibility, source localization, LLM fix suggestions, per-finding repro scripts.
- Single-file HTML report plus machine-readable `report.json`; `--ci --fail-on` gate for CI pipelines.
- Multi-role auth via captured storage state, with an opt-in cross-role access check.
- ML/GenAI testing: `qaura ml test` (trained-model artifact suite), `qaura ml probe` (live endpoint adversarial testing), `qaura ml data` (schema/drift/leakage), `qaura ml genai` (chat-feature injection/jailbreak/PII probing).

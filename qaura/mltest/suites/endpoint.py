"""Endpoint suite: probes a live inference (or any JSON) HTTP endpoint with
malformed/adversarial payloads, checks latency tails, and checks for nondeterminism
across repeated identical requests — plan's four-suite breakdown, suite #3.

Uses httpx (already a transitive project dependency, confirmed in Phase 5). No
target-specific knowledge needed beyond a URL, HTTP method, and one known-good
"base payload" to mutate — the malformed-payload cases are generic enough to apply
to any JSON API, not just ML inference specifically.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from qaura.reporting.models import Evidence, Finding, Severity

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_LATENCY_P95_THRESHOLD_MS = 2000.0
DEFAULT_REPEAT_COUNT = 5
OVERSIZED_STRING_LENGTH = 200_000


def _malformed_cases(base_payload: dict) -> dict[str, object]:
    """Each value is either a dict (sent as JSON) or a raw string (sent as the
    literal request body, for the invalid-JSON case)."""
    cases: dict[str, object] = {
        "empty_object": {},
        "null_values": {k: None for k in base_payload},
        "wrong_type_for_all_fields": {k: 999999 for k in base_payload},
        "invalid_json_body": "{not valid json,,,",
    }
    if base_payload:
        cases["oversized_string_field"] = {
            k: ("A" * OVERSIZED_STRING_LENGTH if isinstance(v, str) else v)
            for k, v in base_payload.items()
        }
    return cases


@dataclass
class ProbeResult:
    findings: list[Finding]
    latencies_ms: list[float] = field(default_factory=list)


async def probe_endpoint(
    url: str,
    base_payload: dict,
    method: str = "POST",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    latency_p95_threshold_ms: float = DEFAULT_LATENCY_P95_THRESHOLD_MS,
    repeat_count: int = DEFAULT_REPEAT_COUNT,
    check_determinism: bool = True,
) -> ProbeResult:
    findings: list[Finding] = []

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for case_name, payload in _malformed_cases(base_payload).items():
            try:
                if isinstance(payload, str):
                    resp = await client.request(
                        method, url, content=payload, headers={"Content-Type": "application/json"},
                    )
                else:
                    resp = await client.request(method, url, json=payload)
            except httpx.TimeoutException:
                findings.append(Finding(
                    title=f"Endpoint timeout on malformed input: {case_name}",
                    detector="ml_endpoint", severity=Severity.HIGH, url=url,
                    description=f"Request with payload case {case_name!r} did not respond within {timeout_seconds}s.",
                    evidence=Evidence(),
                ))
                continue
            except httpx.RequestError as e:
                findings.append(Finding(
                    title=f"Endpoint connection error on malformed input: {case_name}",
                    detector="ml_endpoint", severity=Severity.MEDIUM, url=url,
                    description=f"Payload case {case_name!r} raised a connection-level error: {e}",
                    evidence=Evidence(),
                ))
                continue

            if resp.status_code >= 500:
                findings.append(Finding(
                    title=f"Server error on malformed input: {case_name} -> {resp.status_code}",
                    detector="ml_endpoint", severity=Severity.HIGH, url=url,
                    description=(
                        f"{method} {url} with payload case {case_name!r} returned HTTP "
                        f"{resp.status_code} — an unhandled server error rather than a "
                        f"graceful validation failure (4xx)."
                    ),
                    evidence=Evidence(network_failures=[f"{method} {url} [{case_name}] -> {resp.status_code}"]),
                ))

        # Repeated identical requests: latency tail + nondeterminism.
        latencies: list[float] = []
        response_bodies: list[str] = []
        for _ in range(repeat_count):
            start = time.monotonic()
            try:
                resp = await client.request(method, url, json=base_payload)
                latencies.append((time.monotonic() - start) * 1000)
                response_bodies.append(resp.text)
            except httpx.RequestError:
                continue

        if latencies and max(latencies) > latency_p95_threshold_ms:
            findings.append(Finding(
                title=f"High latency: {max(latencies):.0f}ms exceeds {latency_p95_threshold_ms:.0f}ms threshold",
                detector="ml_endpoint", severity=Severity.MEDIUM, url=url,
                description=f"Latencies across {len(latencies)} identical requests (ms): {[round(l) for l in latencies]}",
                evidence=Evidence(),
            ))

        if check_determinism and len(set(response_bodies)) > 1:
            findings.append(Finding(
                title=f"Nondeterministic response: {len(set(response_bodies))} distinct outputs for identical input",
                detector="ml_endpoint", severity=Severity.MEDIUM, url=url,
                description=(
                    f"Sent the same payload {len(response_bodies)} times and got "
                    f"{len(set(response_bodies))} distinct response bodies. If this endpoint is "
                    f"meant to be deterministic (most classifiers/regressors are, for the same "
                    f"input), this indicates non-reproducible inference — a real problem for "
                    f"debugging, auditing, or caching. If some randomness is intentional "
                    f"(e.g. sampling), this check isn't applicable — pass check_determinism=False."
                ),
                evidence=Evidence(),
            ))

        return ProbeResult(findings=findings, latencies_ms=latencies)

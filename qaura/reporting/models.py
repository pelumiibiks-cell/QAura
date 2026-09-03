"""Finding/Evidence/RunReport — the shape every detector (Phase 2 crash/console/network,
Phase 4 invariants/visual/a11y/security/performance) produces, and reporting/html.py
(and later analysis/dedupe.py, analysis/triage.py) consumes. Kept provider/detector
agnostic on purpose — nothing here imports Playwright or google-genai.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ReproStatus(str, Enum):
    """Phase D: distinguishes "never checked" from "checked and failed to reproduce"
    from "doesn't apply to this kind of finding" — the report used to collapse all
    three into a bare reproducibility fraction (or nothing at all), so a visual/a11y
    finding that replay structurally never re-checks read as `0/2, not confirmed`,
    which looks exactly like a real failure to reproduce a real bug. See
    analysis/replay.py and cli.py's `_analyze` for where each value is set."""

    PENDING = "pending"                  # replay not attempted (e.g. --replay-attempts 0)
    NOT_APPLICABLE = "not_applicable"    # this finding's detector isn't replayable at all
    CONFIRMED = "confirmed"              # reproduced on every attempt
    FLAKY = "flaky"                      # reproduced on some but not all attempts
    NOT_REPRODUCED = "not_reproduced"    # genuinely re-checked, reproduced on 0 attempts


@dataclass
class ReproStep:
    """One step in the sequence that produced the finding — enough to replay it
    (Phase 5) or hand-write a Playwright repro from, without needing the full
    orchestrator trace."""

    description: str          # human-readable, e.g. "click e3 'Add to cart'"
    action_kind: str | None = None
    ref: str | None = None
    value: str | None = None
    url_before: str | None = None


@dataclass
class Evidence:
    """Everything a Recorder (browser/recorder.py) can hand over, plus screenshot/trace
    paths once written to disk. All optional — a crash finding might have a screenshot
    and no console errors; a console finding might have neither."""

    console_errors: list[str] = field(default_factory=list)
    network_failures: list[str] = field(default_factory=list)
    page_error: str | None = None
    screenshot_path: str | None = None
    trace_path: str | None = None


@dataclass
class Finding:
    id: str = field(default_factory=lambda: f"BUG-{uuid.uuid4().hex[:8]}")
    title: str = ""
    detector: str = ""              # e.g. "crash", "console", "network", "invariant"
    severity: Severity = Severity.MEDIUM
    persona: str = "heuristic"      # which persona/mode found it; "heuristic" pre-Phase-3
    url: str = ""
    description: str = ""
    repro_steps: list[ReproStep] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)
    likely_component: str | None = None   # filled in by analysis/localize.py, Phase 5
    suggested_fix: str | None = None      # filled in by analysis/fix.py, Phase 5
    confirmed: bool | None = None         # filled in by analysis/replay.py, Phase 5
    reproducibility: str | None = None    # e.g. "5/5", filled in by replay.py
    reproducibility_status: ReproStatus = ReproStatus.PENDING  # Phase D — see ReproStatus
    occurrence_count: int = 1             # filled in by analysis/dedupe.py — how many
                                            # near-duplicate findings this one represents
    repro_script_path: str | None = None  # filled in by cli.py after reporting/repro.py
                                            # emits the runnable pytest regression test
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["reproducibility_status"] = self.reproducibility_status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        """Inverse of to_dict() — reconstructs a Finding from a saved report.json
        entry. Needed by `qaura replay`/`qaura report` (Phase 7), which operate on
        a PAST run's saved JSON rather than an in-memory RunReport."""
        kwargs = dict(d)
        kwargs["severity"] = Severity(d["severity"])
        kwargs["evidence"] = Evidence(**d.get("evidence") or {})
        kwargs["repro_steps"] = [ReproStep(**s) for s in d.get("repro_steps") or []]
        if "reproducibility_status" in kwargs and kwargs["reproducibility_status"] is not None:
            kwargs["reproducibility_status"] = ReproStatus(kwargs["reproducibility_status"])
        else:
            kwargs.pop("reproducibility_status", None)  # older saved reports predate this field
        return cls(**kwargs)


@dataclass
class RunSummary:
    target_url: str
    started_at: str
    finished_at: str
    mode: str                        # "heuristic" | "gemini"
    personas: list[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)   # from core/state.py StateGraph.coverage_summary()
    llm_usage: dict | None = None    # from llm/budget.py Budget.summary(), None in heuristic mode
    trace_paths: dict[str, str] = field(default_factory=dict)  # persona -> trace.zip, only with --trace


@dataclass
class RunReport:
    summary: RunSummary
    findings: list[Finding] = field(default_factory=list)

    def by_severity(self) -> dict[str, list[Finding]]:
        buckets: dict[str, list[Finding]] = {s.value: [] for s in Severity}
        for f in self.findings:
            buckets[f.severity.value].append(f)
        return buckets

    def to_json(self) -> str:
        return json.dumps({
            "summary": asdict(self.summary),
            "findings": [f.to_dict() for f in self.findings],
        }, indent=2, default=str)

    def save_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load_json(cls, path: str | Path) -> "RunReport":
        """Inverse of save_json() — used by `qaura replay`/`qaura report` (Phase 7)
        to operate on a past run without re-crawling."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        summary = RunSummary(**data["summary"])
        findings = [Finding.from_dict(fd) for fd in data["findings"]]
        return cls(summary=summary, findings=findings)

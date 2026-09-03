from qaura.analysis.triage import apply_triage, is_confident_false_positive, triage_finding
from qaura.llm.base import LLMResponse, Tier, Usage
from qaura.llm.schemas import TriageVerdict
from qaura.reporting.models import Finding, Severity


class FakeProvider:
    def __init__(self, verdict: TriageVerdict | None, available: bool = True):
        self._verdict = verdict
        self._available = available
        self.last_call = None

    @property
    def available(self):
        return self._available

    def complete(self, *, tier, system, input, schema=None, images=None, session=None):
        self.last_call = {"tier": tier, "input": input}
        return LLMResponse(text="{}", parsed=self._verdict, usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2), session_id=None)


def _finding(severity=Severity.MEDIUM) -> Finding:
    return Finding(title="x", detector="crash", severity=severity, description="something broke")


async def test_triage_finding_returns_none_when_unavailable():
    provider = FakeProvider(verdict=None, available=False)
    result = await triage_finding(provider, _finding())
    assert result is None


async def test_triage_finding_uses_triage_tier():
    verdict = TriageVerdict(is_likely_false_positive=False, confidence=0.9, severity_adjustment="keep", reasoning="looks real")
    provider = FakeProvider(verdict)
    await triage_finding(provider, _finding())
    assert provider.last_call["tier"] == Tier.TRIAGE


async def test_apply_triage_raises_severity():
    verdict = TriageVerdict(is_likely_false_positive=False, confidence=0.9, severity_adjustment="raise", reasoning="worse than it looks")
    provider = FakeProvider(verdict)
    finding = _finding(severity=Severity.MEDIUM)
    await apply_triage(provider, finding)
    assert finding.severity == Severity.HIGH


async def test_apply_triage_lowers_severity():
    verdict = TriageVerdict(is_likely_false_positive=False, confidence=0.9, severity_adjustment="lower", reasoning="minor")
    provider = FakeProvider(verdict)
    finding = _finding(severity=Severity.MEDIUM)
    await apply_triage(provider, finding)
    assert finding.severity == Severity.LOW


async def test_apply_triage_keeps_severity():
    verdict = TriageVerdict(is_likely_false_positive=False, confidence=0.9, severity_adjustment="keep", reasoning="correct as is")
    provider = FakeProvider(verdict)
    finding = _finding(severity=Severity.MEDIUM)
    await apply_triage(provider, finding)
    assert finding.severity == Severity.MEDIUM


async def test_apply_triage_severity_caps_at_critical():
    verdict = TriageVerdict(is_likely_false_positive=False, confidence=0.9, severity_adjustment="raise", reasoning="critical")
    provider = FakeProvider(verdict)
    finding = _finding(severity=Severity.CRITICAL)
    await apply_triage(provider, finding)
    assert finding.severity == Severity.CRITICAL  # can't go higher than critical


async def test_apply_triage_severity_floors_at_info():
    verdict = TriageVerdict(is_likely_false_positive=True, confidence=0.9, severity_adjustment="lower", reasoning="noise")
    provider = FakeProvider(verdict)
    finding = _finding(severity=Severity.INFO)
    await apply_triage(provider, finding)
    assert finding.severity == Severity.INFO  # can't go lower than info


async def test_apply_triage_appends_reasoning_note():
    verdict = TriageVerdict(is_likely_false_positive=False, confidence=0.85, severity_adjustment="keep", reasoning="this is a real bug")
    provider = FakeProvider(verdict)
    finding = _finding()
    original_description = finding.description
    await apply_triage(provider, finding)
    assert original_description in finding.description
    assert "this is a real bug" in finding.description
    assert "85%" in finding.description


async def test_apply_triage_returns_none_when_unavailable_and_does_not_mutate():
    provider = FakeProvider(verdict=None, available=False)
    finding = _finding(severity=Severity.MEDIUM)
    original_description = finding.description
    result = await apply_triage(provider, finding)
    assert result is None
    assert finding.severity == Severity.MEDIUM
    assert finding.description == original_description


def test_is_confident_false_positive_true_above_threshold():
    verdict = TriageVerdict(is_likely_false_positive=True, confidence=0.85, severity_adjustment="keep", reasoning="x")
    assert is_confident_false_positive(verdict) is True


def test_is_confident_false_positive_false_below_threshold():
    verdict = TriageVerdict(is_likely_false_positive=True, confidence=0.4, severity_adjustment="keep", reasoning="x")
    assert is_confident_false_positive(verdict) is False


def test_is_confident_false_positive_false_when_not_flagged():
    verdict = TriageVerdict(is_likely_false_positive=False, confidence=0.95, severity_adjustment="keep", reasoning="x")
    assert is_confident_false_positive(verdict) is False


def test_is_confident_false_positive_false_when_none():
    assert is_confident_false_positive(None) is False

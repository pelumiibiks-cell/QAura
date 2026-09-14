"""Tests the quota-fallback logic in llm/gemini.py without making live API calls —
a fake client stands in for google.genai's Client, and a real (but not properly
`__init__`'d — that's fine, isinstance() and raise/except don't need it)
RateLimitError instance stands in for what the SDK actually raises on a 429.
"""
import pytest
from google.genai._gaos.lib.compat_errors import RateLimitError

import qaura.llm.gemini as gemini_module
from qaura.config import ModelTiers
from qaura.llm.base import Tier
from qaura.llm.gemini import GeminiProvider


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    # _create_with_retry's exponential backoff is real behavior worth having in
    # production, but these tests want to exercise the retry/quota-fallback LOGIC,
    # not wait through real sleeps for it.
    monkeypatch.setattr(gemini_module, "_RETRY_BACKOFF_SECONDS", 0.0)


def _fake_rate_limit_error() -> RateLimitError:
    err = RateLimitError.__new__(RateLimitError)
    err.message = "Quota exceeded (fake, for testing)"
    return err


class _FakeInteraction:
    def __init__(self, model: str) -> None:
        self.output_text = f"response from {model}"
        self.id = "fake-session"
        self.usage = None


class _FakeInteractionsAPI:
    def __init__(self, fail_models: set[str]) -> None:
        self.fail_models = fail_models
        self.calls: list[str] = []

    def create(self, *, model: str, **kwargs):
        self.calls.append(model)
        if model in self.fail_models:
            raise _fake_rate_limit_error()
        return _FakeInteraction(model)


class _FakeClient:
    def __init__(self, fail_models: set[str]) -> None:
        self.interactions = _FakeInteractionsAPI(fail_models)


def _provider_with_fake_client(fail_models: set[str]) -> tuple[GeminiProvider, _FakeClient]:
    tiers = ModelTiers()  # planner=gemini-3.1-pro-preview, element_classify=gemini-3.5-flash-lite
    provider = GeminiProvider(api_key="fake-key", model_tiers=tiers)
    fake_client = _FakeClient(fail_models)
    provider._client = fake_client  # bypass the lazy genai.Client() construction
    return provider, fake_client


def test_complete_succeeds_normally_when_no_quota_issue():
    provider, client = _provider_with_fake_client(fail_models=set())
    response = provider.complete(tier=Tier.PLANNER, system="be curious", input="hello")
    assert "gemini-3.1-pro-preview" in response.text
    assert client.interactions.calls == ["gemini-3.1-pro-preview"]


def test_complete_falls_back_to_element_classify_on_quota_error():
    tiers = ModelTiers()
    provider, client = _provider_with_fake_client(fail_models={tiers.planner})
    response = provider.complete(tier=Tier.PLANNER, system="be curious", input="hello")
    assert tiers.element_classify in response.text
    assert client.interactions.calls == [tiers.planner, tiers.element_classify]


def test_complete_remembers_downgrade_for_subsequent_calls():
    tiers = ModelTiers()
    provider, client = _provider_with_fake_client(fail_models={tiers.planner})
    provider.complete(tier=Tier.PLANNER, system="s", input="first call")
    provider.complete(tier=Tier.PLANNER, system="s", input="second call")
    # second call should go straight to the fallback model, never retry the blocked one
    assert client.interactions.calls == [tiers.planner, tiers.element_classify, tiers.element_classify]


def test_complete_raises_when_fallback_model_itself_is_quota_blocked():
    tiers = ModelTiers()
    provider, client = _provider_with_fake_client(fail_models={tiers.planner, tiers.element_classify})
    with pytest.raises(RateLimitError):
        provider.complete(tier=Tier.PLANNER, system="s", input="hello")


def test_complete_retries_transient_errors_then_succeeds():
    # Regression: previously there was no retry at all — a single transient
    # ConnectionError killed whichever call site made it. A bounded retry should
    # recover from a failure that clears up on its own within the retry budget.
    class _FlakyThenOk:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, *, model, **kwargs):
            self.calls += 1
            if self.calls < 2:
                raise ConnectionError("transient network blip")
            return _FakeInteraction(model)

    provider = GeminiProvider(api_key="fake-key", model_tiers=ModelTiers())
    fake_client = _FakeClient(fail_models=set())
    flaky = _FlakyThenOk()
    fake_client.interactions = flaky
    provider._client = fake_client

    response = provider.complete(tier=Tier.PLANNER, system="s", input="hello")
    assert flaky.calls == 2
    assert "gemini" in response.text


def test_complete_gives_up_after_max_retries():
    class _AlwaysFails:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, *, model, **kwargs):
            self.calls += 1
            raise ConnectionError("permanently down")

    provider = GeminiProvider(api_key="fake-key", model_tiers=ModelTiers())
    fake_client = _FakeClient(fail_models=set())
    always_fails = _AlwaysFails()
    fake_client.interactions = always_fails
    provider._client = fake_client

    with pytest.raises(ConnectionError):
        provider.complete(tier=Tier.PLANNER, system="s", input="hello")
    assert always_fails.calls == gemini_module._MAX_RETRIES + 1


def test_complete_does_not_downgrade_on_non_quota_errors():
    class _FakeInteractionsAPIRaisingValueError:
        def create(self, *, model, **kwargs):
            raise ValueError("some unrelated bug")

    provider = GeminiProvider(api_key="fake-key", model_tiers=ModelTiers())
    fake_client = _FakeClient(fail_models=set())
    fake_client.interactions = _FakeInteractionsAPIRaisingValueError()
    provider._client = fake_client

    with pytest.raises(ValueError):
        provider.complete(tier=Tier.PLANNER, system="s", input="hello")
    assert Tier.PLANNER not in provider._quota_downgraded


def test_retry_after_quota_fallback_stays_on_the_fallback_model():
    # Regression: the retry loop passed the original model on every attempt, so a
    # transient error on the fallback call sent the retry back to the quota-blocked model
    tiers = ModelTiers()

    class _BlockedThenFlaky:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.fallback_failures = 1

        def create(self, *, model, **kwargs):
            self.calls.append(model)
            if model == tiers.planner:
                raise _fake_rate_limit_error()
            if self.fallback_failures:
                self.fallback_failures -= 1
                raise ConnectionError("transient blip on the fallback call")
            return _FakeInteraction(model)

    provider = GeminiProvider(api_key="fake-key", model_tiers=tiers)
    fake_client = _FakeClient(fail_models=set())
    api = _BlockedThenFlaky()
    fake_client.interactions = api
    provider._client = fake_client

    response = provider.complete(tier=Tier.PLANNER, system="s", input="hello")
    assert tiers.element_classify in response.text
    assert api.calls == [tiers.planner, tiers.element_classify, tiers.element_classify]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status):
    class _ClientError(Exception):
        status_code = status

    class _AlwaysClientError:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, *, model, **kwargs):
            self.calls += 1
            raise _ClientError(f"HTTP {status}")

    provider = GeminiProvider(api_key="fake-key", model_tiers=ModelTiers())
    fake_client = _FakeClient(fail_models=set())
    api = _AlwaysClientError()
    fake_client.interactions = api
    provider._client = fake_client

    with pytest.raises(_ClientError):
        provider.complete(tier=Tier.PLANNER, system="s", input="hello")
    assert api.calls == 1


def test_malformed_structured_output_raises_parse_error_carrying_usage():
    from qaura.llm.base import LLMParseError
    from qaura.llm.schemas import PlannerResponse

    class _NotJson:
        def create(self, *, model, **kwargs):
            interaction = _FakeInteraction(model)
            interaction.output_text = "definitely not json"
            return interaction

    provider = GeminiProvider(api_key="fake-key", model_tiers=ModelTiers())
    fake_client = _FakeClient(fail_models=set())
    fake_client.interactions = _NotJson()
    provider._client = fake_client

    with pytest.raises(LLMParseError) as exc_info:
        provider.complete(tier=Tier.PLANNER, system="s", input="hello", schema=PlannerResponse)
    assert exc_info.value.usage is not None
    assert exc_info.value.text == "definitely not json"

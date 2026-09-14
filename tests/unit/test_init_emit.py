"""YAML emission.

The highest-value tests in this feature. An emitter bug is invisible at write time and
loud much later: the file looks fine, and load_config() refuses it on the user's next
run, by which point the recon that produced it is long gone. So every test here ends by
actually parsing the output and constructing a real QAuraConfig from it.
"""
import pytest
import yaml

from qaura.config import InvariantConfig, QAuraConfig
from qaura.init.candidates import CandidateVerdict
from qaura.init.emit import (
    Annotated,
    ConfidenceLevel,
    ConfigProposal,
    HeaderInfo,
    assert_loadable,
    render_config,
    render_field,
    write_generated,
)


def _header(**overrides):
    base = dict(
        command="qaura init --url http://localhost:8099",
        version="0.1.0", pages_visited=7, distinct_states=5,
        blocked_requests=0, allowed_requests=21, login_verdict="none",
        authorization="Local target; no authorization prompt required.",
        recon_digest="abc123def456", llm_used=True, notes=[],
    )
    base.update(overrides)
    return HeaderInfo(**base)


def _proposal(fields=None, verdicts=None, **header_overrides):
    return ConfigProposal(
        header=_header(**header_overrides),
        fields=fields if fields is not None else [
            Annotated("target_url", "http://localhost:8099/", ConfidenceLevel.HIGH, "The seed URL."),
        ],
        verdicts=verdicts or [],
    )


def test_output_parses_and_constructs_a_config():
    rendered = render_config(_proposal(fields=[
        Annotated("target_url", "http://localhost:8099/", ConfidenceLevel.HIGH, "Seed."),
        Annotated("guardrails", {"allowed_domains": ["localhost"], "max_actions_per_run": 200},
                  ConfidenceLevel.HIGH, "Scope."),
        Annotated("personas", {"enabled": ["curious"]}, ConfidenceLevel.MEDIUM, "Picked."),
    ]))
    data = yaml.safe_load(rendered)
    config = QAuraConfig(**data)
    assert config.target_url == "http://localhost:8099/"
    assert config.guardrails.allowed_domains == ["localhost"]
    assert config.personas.enabled == ["curious"]


def test_every_field_is_preceded_by_a_confidence_comment():
    rendered = render_config(_proposal(fields=[
        Annotated("target_url", "http://x/", ConfidenceLevel.HIGH, "Seed."),
        Annotated("admin_paths", [], ConfidenceLevel.HIGH, "None found."),
    ]))
    assert "# target_url — confidence: high" in rendered
    assert "# admin_paths — confidence: high" in rendered


def test_low_confidence_fields_are_commented_out():
    """The core promise: a guess never lands in live config."""
    rendered = render_config(_proposal(fields=[
        Annotated("target_url", "http://x/", ConfidenceLevel.HIGH, "Seed."),
        Annotated("admin_paths", ["/admin"], ConfidenceLevel.LOW, "Guessed from the path name."),
    ]))
    data = yaml.safe_load(rendered)
    assert "admin_paths" not in data
    assert "# admin_paths:" in rendered
    QAuraConfig(**data)


def test_awkward_values_round_trip():
    """A selector with quotes and a description with a colon and a hash both need real
    quoting rules. This is why values go through yaml.safe_dump instead of an f-string."""
    selector = '[data-testid="cart-total"]'
    description = "Total: must equal subtotal - discount # including tax"
    rendered = render_config(_proposal(fields=[
        Annotated("target_url", "http://x/", ConfidenceLevel.HIGH, "Seed."),
        Annotated("guardrails", {"blocked_paths": [selector, description]},
                  ConfidenceLevel.HIGH, "Odd but legal."),
    ]))
    data = yaml.safe_load(rendered)
    assert data["guardrails"]["blocked_paths"] == [selector, description]


def test_subcomments_are_interleaved_without_breaking_parsing():
    rendered = render_field(Annotated(
        "guardrails", {"allowed_domains": ["localhost"], "allow_destructive": False},
        ConfidenceLevel.HIGH, "Scope and budget.",
        subcomments={"allowed_domains": "Hostname of the final URL.",
                     "allow_destructive": "Never inferred."},
    ))
    assert "Hostname of the final URL." in rendered
    assert "Never inferred." in rendered
    data = yaml.safe_load(rendered)
    assert data["guardrails"]["allowed_domains"] == ["localhost"]
    assert data["guardrails"]["allow_destructive"] is False


def test_accepted_invariant_is_emitted_live():
    verdict = CandidateVerdict(
        invariant=InvariantConfig(
            name="total_matches", description="Total equals subtotal minus discount",
            container_selector='[data-testid="cart"]',
            values={"total": '[data-testid="cart-total"]',
                    "subtotal": '[data-testid="cart-subtotal"]'},
            expression="abs(total - subtotal) <= 0.01",
        ),
        status="accepted", conclusive_count=4, holds_count=4,
        reason="held on all 4 page(s)",
    )
    rendered = render_config(_proposal(verdicts=[verdict]))
    data = yaml.safe_load(rendered)
    assert len(data["invariants"]) == 1
    assert data["invariants"][0]["name"] == "total_matches"
    QAuraConfig(**data)


def test_single_observation_invariant_is_commented_out():
    """One data point is a coincidence until it isn't."""
    verdict = CandidateVerdict(
        invariant=InvariantConfig(
            name="thin_evidence", description="Only seen once",
            values={"a": "#a", "b": "#b"}, expression="a >= b",
        ),
        status="accepted", conclusive_count=1, holds_count=1, reason="held on all 1 page(s)",
    )
    rendered = render_config(_proposal(verdicts=[verdict]))
    data = yaml.safe_load(rendered)
    assert data["invariants"] == []
    assert "thin_evidence" in rendered  # kept, visibly, as a comment


def test_violated_invariant_is_commented_with_its_evidence():
    """A violation means either a bad rule or a real bug already in the app, and the
    reader needs enough to tell which before deleting it."""
    verdict = CandidateVerdict(
        invariant=InvariantConfig(name="broken", description="d",
                                  values={"a": "#a", "b": "#b"}, expression="a == b"),
        status="rejected_violated", conclusive_count=3, holds_count=1,
        sample_values={"a": 10.0, "b": 4.0},
        violating_state_url="http://localhost:8099/cart",
        reason="held on 1 of 3 page(s)",
    )
    rendered = render_config(_proposal(verdicts=[verdict]))
    assert yaml.safe_load(rendered)["invariants"] == []
    assert "REJECTED" in rendered
    assert "http://localhost:8099/cart" in rendered
    assert "10.0" in rendered


def test_unverified_invariant_keeps_its_error():
    verdict = CandidateVerdict(
        invariant=InvariantConfig(name="never_seen", description="d",
                                  values={"a": "#a", "b": "#b"}, expression="a == b"),
        status="unverified",
        reason="values['a'] selector '#a' matched nothing",
    )
    rendered = render_config(_proposal(verdicts=[verdict]))
    assert yaml.safe_load(rendered)["invariants"] == []
    assert "unverified" in rendered
    assert "matched nothing" in rendered


def test_durability_is_reported_per_rule():
    verdict = CandidateVerdict(
        invariant=InvariantConfig(name="fragile_rule", description="d",
                                  values={"a": "#a", "b": "#b"}, expression="a >= b"),
        status="accepted", conclusive_count=3, holds_count=3, reason="held",
        selector_kinds={"a": "structural", "b": "testid"},
        selector_scores={"a": 20, "b": 110},
    )
    assert verdict.durability == "fragile"
    rendered = render_config(_proposal(verdicts=[verdict]))
    assert "fragile" in rendered
    assert "structural" in rendered


def test_assert_loadable_rejects_a_dangerous_expression():
    """The gate that makes an emitter escaping bug impossible to ship."""
    verdict = CandidateVerdict(
        invariant=InvariantConfig(name="evil", description="d",
                                  values={"a": "#a", "b": "#b"},
                                  expression="__import__('os').system('echo pwned')"),
        status="accepted", conclusive_count=5, holds_count=5, reason="held",
    )
    rendered = render_config(_proposal(verdicts=[verdict]))
    with pytest.raises(ValueError, match="engine rejects"):
        assert_loadable(rendered)


def test_assert_loadable_accepts_a_real_proposal():
    assert_loadable(render_config(_proposal(fields=[
        Annotated("target_url", "http://localhost:8099/", ConfidenceLevel.HIGH, "Seed."),
        Annotated("guardrails", {"allowed_domains": ["localhost"]}, ConfidenceLevel.HIGH, "Scope."),
    ])))


def test_header_records_the_read_only_evidence():
    rendered = render_config(_proposal(blocked_requests=3, allowed_requests=40))
    assert "GET/HEAD only" in rendered
    assert "3 blocked" in rendered
    assert "abc123def456" in rendered
    assert "never writes to qaura.yaml" in rendered


def test_write_refuses_to_overwrite_without_force(tmp_path):
    target = tmp_path / "gen.yaml"
    write_generated("target_url: http://x/\n", target)
    with pytest.raises(FileExistsError):
        write_generated("target_url: http://y/\n", target)
    write_generated("target_url: http://y/\n", target, force=True)
    assert "http://y/" in target.read_text(encoding="utf-8")

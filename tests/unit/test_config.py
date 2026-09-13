from pathlib import Path

import pytest

from qaura.config import ConfigError, QAuraConfig, load_config


def test_defaults_load_without_any_yaml_or_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.gemini_api_key is None
    assert cfg.effective_llm_mode == "heuristic"
    assert cfg.output_dir == "runs"
    assert "curious" in cfg.personas.enabled


def test_llm_mode_auto_switches_to_gemini_when_key_present(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.has_llm_credential is True
    assert cfg.effective_llm_mode == "gemini"


def test_llm_mode_can_be_forced_to_heuristic_even_with_key(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.chdir(tmp_path)
    cfg = QAuraConfig(llm_mode="heuristic")
    assert cfg.effective_llm_mode == "heuristic"


def test_yaml_config_is_picked_up(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text(
        "target_url: http://localhost:9999\noutput_dir: custom_runs\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.target_url == "http://localhost:9999"
    assert cfg.output_dir == "custom_runs"


def test_guardrail_defaults_are_safe(monkeypatch, tmp_path):
    # Regression: this was the one test in the file that didn't chdir into a tmp_path
    # or clear GEMINI_API_KEY, so QAuraConfig() picked up this machine's real .env
    # and environment instead of a clean default — it happened to still pass (it
    # only asserts on guardrail fields), but for the wrong reason.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = QAuraConfig()
    assert cfg.guardrails.allow_destructive is False
    assert cfg.guardrails.max_actions_per_run > 0
    assert "delete" in cfg.guardrails.destructive_patterns


def test_malformed_yaml_raises_config_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text("target_url: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_yaml_with_invalid_field_value_raises_config_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text("seed: not_a_number\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_yaml_that_is_not_a_mapping_raises_config_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_env_var_overrides_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text(
        "output_dir: from_yaml\ntarget_url: http://yaml.test\n", encoding="utf-8",
    )
    monkeypatch.setenv("QAURA_OUTPUT_DIR", "from_env")
    cfg = load_config()
    assert cfg.output_dir == "from_env"
    assert cfg.target_url == "http://yaml.test"


def test_gemini_key_from_env_overrides_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text("GEMINI_API_KEY: from-yaml\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert load_config().gemini_api_key == "from-env"


def test_typo_in_nested_yaml_key_raises_config_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text("guardrails:\n  allowed_domain: [shop.test]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="allowed_domain"):
        load_config()


def test_unknown_top_level_yaml_key_warns(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text("target_urll: http://x.test\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="qaura.config"):
        cfg = load_config()
    assert cfg.target_url is None
    assert "target_urll" in caplog.text


def test_invalid_invariant_expression_raises_config_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "qaura.yaml").write_text(
        "invariants:\n"
        "  - name: cart_total\n"
        "    description: d\n"
        "    values: {total: '#t'}\n"
        "    expression: 'totl > 0'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="cart_total"):
        load_config()


def test_allowed_domains_are_lowercased():
    from qaura.config import GuardrailConfig

    assert GuardrailConfig(allowed_domains=["Shop.TEST"]).allowed_domains == ["shop.test"]


def test_example_config_still_loads(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    example = Path(__file__).resolve().parents[2] / "qaura.example.yaml"
    cfg = load_config(example)
    assert len(cfg.invariants) == 2

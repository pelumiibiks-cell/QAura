import ast

from qaura.config import InvariantConfig
from qaura.reporting.models import Finding, ReproStep
from qaura.reporting.repro import emit_repro_script, render_repro_script


def _finding_with_steps() -> Finding:
    return Finding(
        id="BUG-abc12345",
        title="Uncaught page error: TypeError",
        detector="crash",
        url="http://127.0.0.1:8099/",
        description="widget.activate() called on undefined",
        repro_steps=[
            ReproStep(description="click e10 button 'Trigger broken widget'",
                      action_kind="click", ref="e10", url_before="http://127.0.0.1:8099/"),
        ],
    )


def test_render_produces_syntactically_valid_python():
    script = render_repro_script(_finding_with_steps())
    ast.parse(script)  # raises SyntaxError if invalid — this is the real assertion


def test_render_embeds_finding_fields():
    script = render_repro_script(_finding_with_steps())
    assert "BUG-abc12345" in script
    assert "Trigger broken widget" in script
    assert "click" in script


def test_render_includes_regression_assertion():
    script = render_repro_script(_finding_with_steps())
    assert "result.successes == 0" in script
    assert "replay_finding" in script


def test_render_with_no_repro_steps_still_valid_python():
    finding = Finding(id="BUG-empty", title="x", detector="crash", url="http://x/")
    script = render_repro_script(finding)
    ast.parse(script)
    assert "no repro steps captured" in script


def test_render_invariant_finding_embeds_matching_config():
    finding = Finding(
        id="BUG-inv001", title="Invariant violated: total_reflects_discount",
        detector="invariant", url="http://x/",
        repro_steps=[ReproStep(description="fill e2", action_kind="fill", ref="e2", value="SAVE10")],
    )
    inv = InvariantConfig(
        name="total_reflects_discount", description="d", container_selector="[data-testid=cart]",
        values={"total": "[data-testid=cart-total]"}, expression="total > 0",
    )
    script = render_repro_script(finding, invariants=[inv])
    ast.parse(script)
    assert "total_reflects_discount" in script
    assert "[data-testid=cart-total]" in script
    assert "NOTE:" not in script  # matched successfully, no warning comment


def test_render_invariant_finding_without_matching_config_warns():
    finding = Finding(
        id="BUG-inv002", title="Invariant violated: some_other_rule",
        detector="invariant", url="http://x/",
        repro_steps=[ReproStep(description="x", action_kind="click", ref="e1")],
    )
    script = render_repro_script(finding, invariants=[])
    ast.parse(script)
    assert "NOTE:" in script
    assert "cannot actually replay" in script


def test_emit_writes_file_and_returns_path(tmp_path):
    finding = _finding_with_steps()
    path = emit_repro_script(finding, tmp_path)
    assert path is not None
    assert path.exists()
    assert path.name == "test_repro_BUG_abc12345.py"
    ast.parse(path.read_text(encoding="utf-8"))


def test_emit_returns_none_for_finding_with_no_repro_steps(tmp_path):
    finding = Finding(id="BUG-empty", title="x", detector="crash", url="http://x/")
    path = emit_repro_script(finding, tmp_path)
    assert path is None


def test_render_with_hostile_title_and_description_stays_valid_python():
    # Regression: title/description carry target-controlled content (console text,
    # DOM text, an LLM triage note) and used to be interpolated RAW into a `"""`
    # docstring — a stray triple-quote or trailing backslash broke the generated
    # file's syntax, and text after a closing `"""` became executable module code.
    finding = Finding(
        id="BUG-hostile",
        title='Console error: """; import os; os.system("rm -rf /") #',
        detector="crash",
        url="http://x/",
        description="trailing backslash at end of line \\",
        repro_steps=[ReproStep(description="click e1", action_kind="click", ref="e1")],
    )
    script = render_repro_script(finding)
    ast.parse(script)  # would raise SyntaxError if the hostile text broke the docstring
    # The hostile text still ends up in the file (safely, as a Python string literal
    # via FINDING.title's !r formatting), it just isn't allowed to break the syntax.
    assert "os.system" in script


def test_safe_id_strips_path_traversal(tmp_path):
    # Regression: RunReport.load_json() accepts any `id` string from a report.json on
    # disk (qaura report / qaura replay take a user-supplied path to one), and the old
    # safe_id only replaced "-" and " ", leaving "/", "\\", and ".." untouched.
    finding = Finding(
        id="../../etc/BUG-1",
        title="x", detector="crash", url="http://x/",
        repro_steps=[ReproStep(description="click e1", action_kind="click", ref="e1")],
    )
    path = emit_repro_script(finding, tmp_path)
    assert path is not None
    assert path.parent == tmp_path  # written inside out_dir, not above it
    assert ".." not in path.name
    assert "/" not in path.name and "\\" not in path.name

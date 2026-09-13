"""Single-file HTML report. Everything inlined (no external CSS/JS/image references)
so the report is one file you can email or drop in a shared folder and it still
renders correctly. Screenshots referenced by path are read and base64-inlined at
render time — a report you can move away from the run directory and it still works.
"""
from __future__ import annotations

import base64
from pathlib import Path

from jinja2 import Environment, BaseLoader, select_autoescape

from qaura.reporting.models import Finding, ReproStatus, RunReport, Severity

# CRITICAL first — Phase F fix. The old template rendered report.findings in
# whatever order dedupe happened to leave them (first-appearance order, by
# design — see analysis/dedupe.py), so a CRITICAL finding could render below a
# LOW one. by_severity() already existed but was only ever used for the count
# tiles, never to order the finding list itself.
_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}

_REPRO_LABELS = {
    ReproStatus.CONFIRMED: ("confirmed", "repro-confirmed"),
    ReproStatus.FLAKY: ("flaky", "repro-flaky"),
    ReproStatus.NOT_REPRODUCED: ("not reproduced", "repro-not-reproduced"),
    ReproStatus.NOT_APPLICABLE: ("not re-checked by replay", "repro-na"),
    ReproStatus.PENDING: ("not yet replayed", "repro-na"),
}

_TEMPLATE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>QAura report — {{ report.summary.target_url }}</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 960px;
         margin: 0 auto; padding: 24px; line-height: 1.5; }
  h1 { font-size: 1.4rem; }
  .meta { color: #666; font-size: 0.9rem; margin-bottom: 24px; }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr));
                  gap: 8px; margin-bottom: 16px; }
  .stat { border: 1px solid #ddd; border-radius: 6px; padding: 10px 12px; }
  .stat .n { font-size: 1.3rem; font-weight: 600; }
  .stat .l { font-size: 0.8rem; color: #666; }
  .filter-bar { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center;
                border: 1px solid #ddd; border-radius: 6px; padding: 10px 12px; margin-bottom: 24px;
                font-size: 0.85rem; }
  .filter-bar strong { font-size: 0.8rem; color: #666; margin-right: 4px; }
  .filter-bar label { cursor: pointer; user-select: none; }
  .finding { border: 1px solid #ddd; border-radius: 8px; margin-bottom: 16px; padding: 14px 16px;
             scroll-margin-top: 12px; }
  .finding h3 { margin: 0 0 4px 0; font-size: 1.05rem; }
  .badge { display: inline-block; border-radius: 4px; padding: 1px 8px; font-size: 0.75rem;
           font-weight: 600; text-transform: uppercase; margin-right: 6px; }
  .sev-critical { background: #7f1d1d; color: #fff; }
  .sev-high { background: #b91c1c; color: #fff; }
  .sev-medium { background: #b45309; color: #fff; }
  .sev-low { background: #365314; color: #fff; }
  .sev-info { background: #374151; color: #fff; }
  .detector-badge { background: #1e3a5f; color: #fff; }
  .repro-confirmed { background: #7f1d1d; color: #fff; }
  .repro-flaky { background: #92400e; color: #fff; }
  .repro-not-reproduced { background: #4b5563; color: #fff; }
  .repro-na { background: #6b7280; color: #fff; }
  .occurrence-badge { background: #374151; color: #fff; }
  .run-notes { border-left: 3px solid #b45309; padding: 4px 12px; margin-bottom: 16px; font-size: 0.9rem; }
  .run-notes ul { margin: 4px 0; padding-left: 18px; }
  .anchor-link { color: inherit; text-decoration: none; }
  .anchor-link:hover { text-decoration: underline; }
  pre { background: #f5f5f5; padding: 8px 10px; border-radius: 6px; overflow-x: auto;
        font-size: 0.85rem; white-space: pre-wrap; }
  .steps { margin: 8px 0; padding-left: 20px; }
  .evidence-block { margin-top: 8px; }
  .evidence-block summary { cursor: pointer; font-size: 0.85rem; color: #444; }
  img.screenshot { max-width: 100%; border: 1px solid #ccc; border-radius: 4px; margin-top: 6px;
                   cursor: zoom-in; }
  img.screenshot.zoomed { max-width: none; width: 100%; cursor: zoom-out; }
  @media (prefers-color-scheme: dark) {
    body { background: #111; color: #eee; }
    .meta, .l, .filter-bar strong { color: #999; }
    .stat, .finding, .filter-bar { border-color: #333; }
    pre { background: #1a1a1a; color: #ddd; }
    .evidence-block summary { color: #bbb; }
  }
</style>
</head>
<body>
<h1>QAura report</h1>
<div class="meta">
  Target: {{ report.summary.target_url }} &middot;
  Mode: {{ report.summary.mode }} &middot;
  {{ report.summary.started_at }} &rarr; {{ report.summary.finished_at }}
  {% if report.summary.personas %} &middot; Personas: {{ report.summary.personas | join(", ") }}{% endif %}
  {% if report.summary.llm_usage %} &middot; LLM calls: {{ report.summary.llm_usage.get("calls", 0) }}
    ({{ report.summary.llm_usage.get("total_tokens", 0) }} tokens){% endif %}
</div>

{% if report.summary.notes %}
<div class="run-notes"><strong>This run ended early or partly failed:</strong>
  <ul>{% for note in report.summary.notes %}<li>{{ note }}</li>{% endfor %}</ul>
</div>
{% endif %}

<div class="summary-grid">
  <div class="stat"><div class="n">{{ report.findings | length }}</div><div class="l">total findings</div></div>
  {% for sev, items in by_sev.items() %}
  {% if items %}
  <div class="stat"><div class="n">{{ items | length }}</div><div class="l">{{ sev }}</div></div>
  {% endif %}
  {% endfor %}
  {# ml/report.py:build_run_report() also stuffs ML metrics (accuracy, etc.) into
     this same `coverage` field for JSON persistence, since RunSummary has nowhere
     else to put them — those dicts are truthy but have no "total_elements" key, so
     a plain truthiness check here used to render every ML report's coverage tiles
     as a misleading "0 states explored / 0 of 0 elements exercised". Only render
     for a dict that's actually crawl-coverage-shaped. #}
  {% if report.summary.coverage and "total_elements" in report.summary.coverage %}
  <div class="stat"><div class="n">{{ report.summary.coverage.get("states", 0) }}</div><div class="l">states explored</div></div>
  <div class="stat"><div class="n">{{ report.summary.coverage.get("exercised_elements", 0) }} / {{ report.summary.coverage.get("total_elements", 0) }}</div><div class="l">elements exercised</div></div>
  <div class="stat"><div class="n">{{ report.summary.coverage.get("unexplored_states", 0) }}</div><div class="l">states with unreached elements</div></div>
  {% endif %}
</div>

{% if report.findings %}
<div class="filter-bar" id="filter-bar">
  <strong>Severity</strong>
  {% for sev in ["critical","high","medium","low","info"] %}{% if by_sev.get(sev) %}
  <label><input type="checkbox" class="f-sev" value="{{ sev }}" checked> {{ sev }} ({{ by_sev[sev] | length }})</label>
  {% endif %}{% endfor %}
  <strong style="margin-left: 10px;">Detector</strong>
  {% for det in detectors %}
  <label><input type="checkbox" class="f-det" value="{{ det }}" checked> {{ det }}</label>
  {% endfor %}
</div>
{% endif %}

{% set unreached = report.summary.coverage.get("unreached") if report.summary.coverage else None %}
{% if unreached %}
<details class="evidence-block" style="margin-bottom: 24px;">
  <summary><strong>{{ unreached | length }} element(s) never interacted with</strong> — what this run didn't reach</summary>
  <pre>{{ unreached | join("\n") }}</pre>
</details>
{% endif %}

{% if not report.findings %}
<p><strong>No findings.</strong></p>
{% endif %}

<div id="findings">
{% for f in sorted_findings %}
<div class="finding" id="{{ f.id }}" data-severity="{{ f.severity.value if f.severity.value else f.severity }}" data-detector="{{ f.detector }}">
  <span class="badge sev-{{ f.severity.value if f.severity.value else f.severity }}">{{ f.severity.value if f.severity.value else f.severity }}</span>
  <span class="badge detector-badge">{{ f.detector }}</span>
  {% if f.occurrence_count and f.occurrence_count > 1 %}<span class="badge occurrence-badge">seen {{ f.occurrence_count }}&times;</span>{% endif %}
  {% set repro_label, repro_class = repro_display(f) %}
  <span class="badge {{ repro_class }}">{{ repro_label }}</span>
  <h3><a class="anchor-link" href="#{{ f.id }}">{{ f.title }}</a></h3>
  <div class="meta">{{ f.id }} &middot; persona: {{ f.persona }} &middot; {{ f.url }}</div>
  <p>{{ f.description }}</p>

  {% if screenshots.get(f.id) %}
  <div><img class="screenshot" src="data:image/png;base64,{{ screenshots[f.id] }}" alt="screenshot for {{ f.id }}" onclick="this.classList.toggle('zoomed')"></div>
  {% endif %}

  {% if f.repro_steps %}
  <div><strong>Repro steps:</strong>
    <ol class="steps">
      {% for step in f.repro_steps %}
      <li>{{ step.description }}</li>
      {% endfor %}
    </ol>
  </div>
  {% endif %}

  {% if f.likely_component %}
  <p><strong>Likely component:</strong> <code>{{ f.likely_component }}</code></p>
  {% endif %}
  {% if f.suggested_fix %}
  <p><strong>Suggested fix:</strong> {{ f.suggested_fix }}</p>
  {% endif %}
  {% if f.repro_script_path %}
  <p><strong>Regression test:</strong> <code>{{ f.repro_script_path }}</code></p>
  {% endif %}

  <details class="evidence-block">
    <summary>Evidence</summary>
    {% if f.evidence.page_error %}<pre>{{ f.evidence.page_error }}</pre>{% endif %}
    {% if f.evidence.console_errors %}<pre>{{ f.evidence.console_errors | join("\n") }}</pre>{% endif %}
    {% if f.evidence.network_failures %}<pre>{{ f.evidence.network_failures | join("\n") }}</pre>{% endif %}
  </details>
</div>
{% endfor %}
</div>

<script>
(function () {
  var bar = document.getElementById("filter-bar");
  if (!bar) return;
  function apply() {
    var sevs = Array.from(document.querySelectorAll(".f-sev:checked")).map(function (el) { return el.value; });
    var dets = Array.from(document.querySelectorAll(".f-det:checked")).map(function (el) { return el.value; });
    document.querySelectorAll(".finding").forEach(function (card) {
      var show = sevs.indexOf(card.dataset.severity) !== -1 && dets.indexOf(card.dataset.detector) !== -1;
      card.hidden = !show;
    });
  }
  bar.addEventListener("change", apply);
})();
</script>

</body>
</html>
"""

_env = Environment(loader=BaseLoader(), autoescape=select_autoescape(["html"]))
_template = _env.from_string(_TEMPLATE)


def _severity_buckets(findings: list[Finding]) -> dict[str, list[Finding]]:
    buckets: dict[str, list[Finding]] = {s.value: [] for s in Severity}
    for f in findings:
        buckets[f.severity.value if hasattr(f.severity, "value") else f.severity].append(f)
    return buckets


def _repro_display(f: Finding) -> tuple[str, str]:
    status = f.reproducibility_status
    if not isinstance(status, ReproStatus):
        status = ReproStatus(status) if status else ReproStatus.PENDING
    label, css_class = _REPRO_LABELS[status]
    if f.reproducibility and status in (ReproStatus.CONFIRMED, ReproStatus.FLAKY, ReproStatus.NOT_REPRODUCED):
        label = f"{label} ({f.reproducibility})"
    return label, css_class


def render_html(report: RunReport) -> str:
    screenshots: dict[str, str] = {}
    for f in report.findings:
        path = f.evidence.screenshot_path
        if path and Path(path).exists():
            screenshots[f.id] = base64.b64encode(Path(path).read_bytes()).decode("ascii")

    sorted_findings = sorted(
        report.findings,
        key=lambda f: _SEVERITY_ORDER.get(f.severity, len(_SEVERITY_ORDER)),
    )
    detectors = sorted({f.detector for f in report.findings})

    return _template.render(
        report=report,
        sorted_findings=sorted_findings,
        detectors=detectors,
        by_sev=_severity_buckets(report.findings),
        screenshots=screenshots,
        repro_display=_repro_display,
    )


def save_html(report: RunReport, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(report), encoding="utf-8")
    return out_path

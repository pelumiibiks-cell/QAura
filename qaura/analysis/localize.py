"""Finding -> candidate source file(s). Extracts search terms straight from what the
finding already carries (URL paths, quoted element names/text from repro steps and
the description) and ranks repo files by substring hits via repo_index.search() — no
LLM needed for this mechanism to work; an LLM refinement pass (disambiguating between
close-scoring candidates, or reading a candidate file to confirm) is a natural future
enhancement but the deterministic version already does the useful part.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from qaura.analysis.repo_index import RepoIndex, search
from qaura.reporting.models import Finding

_QUOTED_RE = re.compile(r"'([^']{2,60})'")


def extract_search_terms(finding: Finding) -> list[str]:
    terms: list[str] = []

    path = urlsplit(finding.url).path
    if path and path != "/":
        terms.append(path)
        last_segment = path.rstrip("/").rsplit("/", 1)[-1]
        if last_segment:
            terms.append(last_segment)

    for nf in finding.evidence.network_failures:
        for url in re.findall(r"https?://\S+", nf):
            p = urlsplit(url).path
            if p and p != "/":
                terms.append(p)

    for step in finding.repro_steps:
        terms.extend(_QUOTED_RE.findall(step.description))

    terms.extend(_QUOTED_RE.findall(finding.description))

    seen: set[str] = set()
    ordered: list[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def localize(index: RepoIndex, finding: Finding, max_results: int = 3) -> list[str]:
    """Returns candidate file paths (relative to the repo root), best match first."""
    terms = extract_search_terms(finding)
    ranked = search(index, terms, max_results=max_results)
    return [index.relative(path) for path, _score in ranked]


def apply_localization(index: RepoIndex, finding: Finding, max_results: int = 3) -> None:
    candidates = localize(index, finding, max_results=max_results)
    if candidates:
        finding.likely_component = ", ".join(candidates)

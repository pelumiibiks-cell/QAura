"""Lightweight source-tree index for finding-to-file localization. Deliberately not
a real parser/AST index — a QA agent needs "which file is probably responsible", not
a full symbol table, and a substring search over a filtered file list gets there for
the vast majority of findings (a URL path, an element's accessible name, or an id
almost always appears verbatim near the responsible code) without needing per-
language tooling.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".qaura", ".qaura-wakeup", "runs", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
# Not a literal dir name, so it can't live in the IGNORE_DIRS set (`"*.egg-info" in
# IGNORE_DIRS` never matches a real path segment like "qaura.egg-info") — checked
# separately in build_index() via endswith().
IGNORE_DIR_SUFFIXES = (".egg-info",)
TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".vue", ".svelte", ".css", ".scss",
    ".json", ".yaml", ".yml", ".go", ".rb", ".java", ".php",
}
MAX_FILE_SIZE_BYTES = 500_000  # skip anything bigger — almost certainly generated/vendored


@dataclass
class RepoIndex:
    root: Path
    files: list[Path]  # absolute paths, already filtered

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.root)).replace("\\", "/")


def build_index(repo_path: str | Path) -> RepoIndex:
    root = Path(repo_path).resolve()
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in IGNORE_DIRS or part.endswith(IGNORE_DIR_SUFFIXES) for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            if p.stat().st_size > MAX_FILE_SIZE_BYTES:
                continue
        except OSError:
            continue
        files.append(p)
    return RepoIndex(root=root, files=files)


def search(index: RepoIndex, terms: list[str], max_results: int = 5) -> list[tuple[Path, int]]:
    """Ranks files by how many distinct `terms` appear in them (case-sensitive exact
    substring — case sensitivity is deliberate: matching "Coupon" against a file that
    only has "coupon" as a lowercase variable name is a weaker signal than an exact
    hit on the visible label/route string, and false-positive-prone term casing
    normalization isn't worth the complexity here). Returns (path, score) pairs,
    highest score first."""
    terms = [t for t in terms if t]
    if not terms:
        return []
    scores: dict[Path, int] = {}
    for f in index.files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        score = sum(1 for t in terms if t in text)
        if score > 0:
            scores[f] = score
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ranked[:max_results]

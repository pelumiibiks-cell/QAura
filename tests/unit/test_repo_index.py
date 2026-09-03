from qaura.analysis.repo_index import build_index, search


def _make_repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes.py").write_text(
        "@app.post('/api/validate-email')\ndef validate_email(): ...\n", encoding="utf-8"
    )
    (tmp_path / "app" / "widget.js").write_text(
        "function brokenHandler() { widget.activate(); }\n", encoding="utf-8"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("should be ignored entirely", encoding="utf-8")
    (tmp_path / "app" / "readme.md").write_text("not a text extension we index", encoding="utf-8")
    return tmp_path


def test_build_index_finds_source_files(tmp_path):
    repo = _make_repo(tmp_path)
    index = build_index(repo)
    rel_paths = {index.relative(p) for p in index.files}
    assert "app/routes.py" in rel_paths
    assert "app/widget.js" in rel_paths


def test_build_index_excludes_ignored_dirs(tmp_path):
    repo = _make_repo(tmp_path)
    index = build_index(repo)
    rel_paths = {index.relative(p) for p in index.files}
    assert not any("node_modules" in p for p in rel_paths)


def test_build_index_excludes_non_text_extensions(tmp_path):
    repo = _make_repo(tmp_path)
    index = build_index(repo)
    rel_paths = {index.relative(p) for p in index.files}
    assert "app/readme.md" not in rel_paths


def test_search_ranks_by_term_hit_count(tmp_path):
    repo = _make_repo(tmp_path)
    index = build_index(repo)
    results = search(index, ["/api/validate-email"])
    assert len(results) == 1
    assert index.relative(results[0][0]) == "app/routes.py"


def test_search_finds_js_handler_by_name(tmp_path):
    repo = _make_repo(tmp_path)
    index = build_index(repo)
    results = search(index, ["brokenHandler"])
    assert len(results) == 1
    assert index.relative(results[0][0]) == "app/widget.js"


def test_search_returns_empty_for_no_matching_terms(tmp_path):
    repo = _make_repo(tmp_path)
    index = build_index(repo)
    assert search(index, ["nonexistent_symbol_xyz"]) == []


def test_search_returns_empty_for_no_terms(tmp_path):
    repo = _make_repo(tmp_path)
    index = build_index(repo)
    assert search(index, []) == []


def test_build_index_excludes_egg_info_dirs(tmp_path):
    # Regression: "*.egg-info" was compared with `in` against a literal path segment
    # ("qaura.egg-info" != "*.egg-info"), so it could never match and egg-info
    # directories were indexed and searched despite being listed as ignored.
    repo = _make_repo(tmp_path)
    egg_info = repo / "qaura.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO.py").write_text("generated metadata, not source", encoding="utf-8")
    index = build_index(repo)
    rel_paths = {index.relative(p) for p in index.files}
    assert not any("egg-info" in p for p in rel_paths)


def test_search_respects_max_results(tmp_path):
    (tmp_path / "a.py").write_text("shared_term", encoding="utf-8")
    (tmp_path / "b.py").write_text("shared_term", encoding="utf-8")
    (tmp_path / "c.py").write_text("shared_term", encoding="utf-8")
    index = build_index(tmp_path)
    results = search(index, ["shared_term"], max_results=2)
    assert len(results) == 2

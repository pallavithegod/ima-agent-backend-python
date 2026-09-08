import pytest

from app.services.fixer import CloneFixerService, FixerError


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pricing.js").write_text("const total = 1;\n")
    (tmp_path / "package.json").write_text("{}\n")
    return tmp_path


def _apply(workspace, fix, logs=""):
    service = CloneFixerService()
    return service._apply_fix(workspace, ["src/pricing.js", "package.json"], fix, logs)


def test_apply_fix_writes_files_and_builds_diff(workspace):
    result = _apply(
        workspace,
        {
            "summary": "fix",
            "files": [
                {"path": "src/pricing.js", "action": "update", "content": "const total = 2;\n"}
            ],
        },
    )
    assert (workspace / "src" / "pricing.js").read_text() == "const total = 2;\n"
    assert "-const total = 1;" in result["diff"]
    assert "+const total = 2;" in result["diff"]
    assert result["primary_path"] == "src/pricing.js"


def test_apply_fix_rejects_path_escape(workspace):
    with pytest.raises(FixerError, match="escapes"):
        _apply(
            workspace,
            {"files": [{"path": "../evil.js", "action": "create", "content": "x"}]},
        )


def test_apply_fix_rejects_git_directory(workspace):
    with pytest.raises(FixerError, match=".git"):
        _apply(
            workspace,
            {"files": [{"path": ".git/config", "action": "update", "content": "x"}]},
        )


def test_apply_fix_rejects_too_many_files(workspace):
    files = [
        {"path": f"file{i}.js", "action": "create", "content": "x\n"} for i in range(9)
    ]
    with pytest.raises(FixerError, match="limit"):
        _apply(workspace, {"files": files})


def test_apply_fix_rejects_lockfiles_for_non_dependency_incidents(workspace):
    with pytest.raises(FixerError, match="lockfile"):
        _apply(
            workspace,
            {"files": [{"path": "package-lock.json", "action": "create", "content": "{}"}]},
            logs="TypeError: item.price is undefined",
        )


def test_apply_fix_allows_lockfiles_for_dependency_incidents(workspace):
    result = _apply(
        workspace,
        {"files": [{"path": "package-lock.json", "action": "create", "content": "{}\n"}]},
        logs="npm error ERESOLVE unable to resolve dependency tree",
    )
    assert result["primary_path"] == "package-lock.json"


def test_apply_fix_rejects_updates_to_missing_files(workspace):
    with pytest.raises(FixerError, match="does not exist"):
        _apply(
            workspace,
            {"files": [{"path": "src/ghost.js", "action": "update", "content": "x\n"}]},
        )


def test_apply_fix_rejects_no_op(workspace):
    with pytest.raises(FixerError, match="does not change"):
        _apply(
            workspace,
            {"files": [{"path": "src/pricing.js", "action": "update", "content": "const total = 1;\n"}]},
        )


def test_candidate_files_prefers_incident_and_log_paths(workspace):
    service = CloneFixerService()
    tree = ["src/pricing.js", "server.js", "package.json"]
    candidates = service._candidate_files(
        {"file_path": "server.js"},
        "TypeError at src/pricing.js:7:9\nnpm error missing dependency",
        tree,
    )
    assert candidates[0] == "server.js"
    assert "src/pricing.js" in candidates
    assert "package.json" in candidates


def test_static_check_rejects_invalid_python(workspace):
    with pytest.raises(FixerError, match="invalid .py"):
        _apply(
            workspace,
            {"files": [{"path": "broken.py", "action": "create", "content": "def broken(:\n"}]},
        )


def test_static_check_rejects_invalid_json(workspace):
    with pytest.raises(FixerError, match="invalid .json"):
        _apply(
            workspace,
            {"files": [{"path": "package.json", "action": "update", "content": "{not json"}]},
        )


def test_candidate_files_strips_runtime_path_prefixes(workspace):
    service = CloneFixerService()
    tree = ["src/pricing.js", "server.js", "package.json"]
    candidates = service._candidate_files(
        {},
        "TypeError at file:///app/src/pricing.js:6:32\n    at /app/server.js:41:19",
        tree,
    )
    assert "src/pricing.js" in candidates
    assert "server.js" in candidates

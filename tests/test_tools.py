import pytest

from agentflow.core.tools import (
    build_default_registry,
    PathViolationError,
    ToolRegistry,
    ensure_within,
)


def test_ensure_within_rejects_traversal(tmp_path):
    with pytest.raises(PathViolationError):
        ensure_within(tmp_path, "../outside.txt")


def test_ensure_within_accepts_inside(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    resolved = ensure_within(tmp_path, "a.txt")
    assert resolved == (tmp_path / "a.txt").resolve()


def test_registry_allowed_tools_by_whitelist():
    registry = build_default_registry()
    config = {"agents": {"executor": {"tools": ["read_artifact"]}}}
    names = [tool.name for tool in registry.allowed_tools("executor", config)]
    assert names == ["read_artifact"]

from __future__ import annotations

from pathlib import Path

from harness.workspace.manager import WorkspaceManager


def test_workspace_manager_isolates_agents(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    first = manager.create_workspace("task", "phase", "planner", "planner-1", 0, 0)
    second = manager.create_workspace("task", "phase", "planner", "planner-2", 0, 0)

    (first.output_dir / "plan.md").write_text("first", encoding="utf-8")
    (second.output_dir / "plan.md").write_text("second", encoding="utf-8")

    assert first.workspace_dir != second.workspace_dir
    assert (first.output_dir / "plan.md").read_text(encoding="utf-8") == "first"
    assert (second.output_dir / "plan.md").read_text(encoding="utf-8") == "second"


def test_workspace_manager_copies_source_repo_without_generated_dirs(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    (source_repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source_repo / "workspaces").mkdir()
    (source_repo / "workspaces" / "old.txt").write_text("ignore", encoding="utf-8")
    (source_repo / ".venv").mkdir()
    (source_repo / ".venv" / "dep.py").write_text("ignore", encoding="utf-8")

    manager = WorkspaceManager(tmp_path / "workspace-root")
    workspace = manager.create_workspace("task", "phase", "executor", "executor-1", 0, 0, source_repo=source_repo)

    assert (workspace.repo_dir / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert not (workspace.repo_dir / "workspaces").exists()
    assert not (workspace.repo_dir / ".venv").exists()

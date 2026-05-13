from __future__ import annotations

from pathlib import Path

from harness.testing.detection import detect_project_profile


def test_python_requires_selects_highest_compatible_bookworm_image(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.9,<3.12"\n',
        encoding="utf-8",
    )

    profile = detect_project_profile(repo, {"testing": {"docker": {}}})

    assert profile.project_type == "python"
    assert profile.image == "python:3.11-bookworm"


def test_node_project_uses_node_default_image(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts":{"test":"node test.js"}}\n', encoding="utf-8")

    profile = detect_project_profile(repo, {"testing": {"docker": {"default_node_image": "node:20-bookworm"}}})

    assert profile.project_type == "node"
    assert profile.image == "node:20-bookworm"


def test_docker_auto_setup_is_opt_in(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    disabled = detect_project_profile(repo, {"testing": {"docker": {"auto_setup": False}}})
    enabled = detect_project_profile(repo, {"testing": {"docker": {"auto_setup": True}}})

    assert disabled.setup_commands == ()
    assert any("command -v python3 || command -v python" in command for command in enabled.setup_commands)
    assert any("-m pip install -r requirements.txt" in command for command in enabled.setup_commands)
    assert any("-m pip install pytest" in command for command in enabled.setup_commands)


def test_project_dockerfile_is_detected_when_allowed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM python:3.11-bookworm\n", encoding="utf-8")

    profile = detect_project_profile(repo, {"testing": {"docker": {"allow_project_dockerfile": True}}})

    assert profile.project_type == "dockerfile"
    assert profile.image == "project-dockerfile"
    assert profile.dockerfile == str(repo / "Dockerfile")

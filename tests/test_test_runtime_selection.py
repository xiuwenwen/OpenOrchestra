from __future__ import annotations

import sys

import pytest

from harness.adapters.command_runner import CapturedCommandResult
from harness.core.errors import TaskFailedError
from harness.gates.test_gate import TestGateService as GateService
from harness.testing.evidence import TestRunEvidence as RunEvidence

class DummyRepository:
    pass

class DummyArtifactManager:
    pass

class DummyCommandRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    def run_capture(self, *args, **kwargs) -> CapturedCommandResult:
        return CapturedCommandResult(returncode=self.returncode, stdout="ok\n", stderr="")

def _service(config: dict, command_runner: DummyCommandRunner | None = None, emit=None) -> GateService:
    return GateService(
        config=config,
        repository=DummyRepository(),  # type: ignore[arg-type]
        artifact_manager=DummyArtifactManager(),  # type: ignore[arg-type]
        latest_materialized_repo=lambda task_id: None,
        markdown_field=lambda content, field: None,
        command_runner=command_runner,
        emit=emit,
    )

@pytest.mark.parametrize(
    ("config", "docker_path", "docker_rc", "expected"),
    [
        ({}, None, 0, "native"),
        ({"testing": {"runtime": "auto", "docker": {"enabled": True}}}, "/usr/bin/docker", 0, "docker"),
        ({"testing": {"runtime": "auto", "docker": {"enabled": True}}}, "/usr/bin/docker", 1, "native"),
        ({"testing": {"runtime": "auto", "docker": {"enabled": False}}}, "/usr/bin/docker", 0, "native"),
        ({"testing": {"runtime": "docker", "docker": {"enabled": True}}}, None, 0, "docker"),
    ],
)
def test_runtime_selection(monkeypatch, config: dict, docker_path: str | None, docker_rc: int, expected: str) -> None:
    monkeypatch.setattr("harness.gates.test_gate.shutil.which", lambda name: docker_path)
    assert _service(config, DummyCommandRunner(returncode=docker_rc)).resolve_test_runtime("task", None) == expected

def _pytest_repo(tmp_path):
    repo = tmp_path / "workspaces" / "task" / "_materialized" / "round_0" / "repo"
    (repo / "pkg" / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return repo

def test_repo_digest_ignores_openorchestra_cache(tmp_path) -> None:
    repo = _pytest_repo(tmp_path)
    service = _service({})
    before = service.repo_content_digest(repo)
    cache_dir = repo / ".openorchestra-cache"
    cache_dir.mkdir()
    (cache_dir / "pip-cache").write_text("generated\n", encoding="utf-8")
    assert service.repo_content_digest(repo) == before
    assert service.repo_has_pytest_tests(repo)

def test_auto_pytest_command_is_runtime_aware(tmp_path) -> None:
    service = _service({})
    native = service.harness_test_commands(_pytest_repo(tmp_path), runtime="native")[0]
    docker = service.harness_test_commands(_pytest_repo(tmp_path), runtime="docker")[0]
    assert (native.command, native.scope) == (f"{sys.executable} -m pytest -q", "host")
    assert (docker.command, docker.scope) == ("python -m pytest -q", "container")
    assert "/Users/" not in docker.command and ".venv/bin/python" not in docker.command

def test_harness_runtime_infra_failure_aborts_test_loop() -> None:
    evidence = RunEvidence(status="fail", runtime="docker", failure_type="infra", notes=("host path leaked",))
    with pytest.raises(TaskFailedError, match="Harness test runtime blocked"):
        _service({}).abort_if_harness_runtime_blocked("test_gate.md", evidence)

def test_harness_env_setup_failure_returns_to_tester_without_abort() -> None:
    evidence = RunEvidence(status="fail", runtime="docker", failure_type="env_setup", notes=("missing dependency",))

    _service({}).abort_if_harness_runtime_blocked("test_gate.md", evidence)

def test_tester_setup_policy_allows_declared_or_minimal_dependencies(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\n[project.optional-dependencies]\ndev = [\"pytest\"]\n",
        encoding="utf-8",
    )
    service = _service({})

    assert service.tester_setup_command_policy_error(repo, "python -m pip install -r requirements.txt") is None
    assert service.tester_setup_command_policy_error(repo, "python -m pip install -e .[dev]") is None
    assert service.tester_setup_command_policy_error(repo, "python -m pip install pytest") is None
    assert "not project-declared" in (
        service.tester_setup_command_policy_error(repo, "python -m pip install pandas") or ""
    )

def test_runtime_selection_event_is_prominent(monkeypatch) -> None:
    monkeypatch.setattr("harness.gates.test_gate.shutil.which", lambda name: None)
    events = []
    runtime, diagnostics = _service({}, emit=events.append).resolve_test_runtime_with_diagnostics("task", None)
    _service({}, emit=events.append).emit_runtime_selection("task", 0, "test_gate.md", diagnostics)
    assert runtime == "native"
    assert events[-1].event_type == "test_runtime_selected"
    assert "[TEST RUNTIME]" in str(events[-1].message)
    assert "requested=auto" in str(events[-1].message)
    assert "selected=native" in str(events[-1].message)

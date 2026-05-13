from __future__ import annotations

from harness.adapters.command_runner import CapturedCommandResult
from harness.gates.test_gate import TestGateService as GateService


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


def test_missing_test_runtime_defaults_to_native_when_docker_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("harness.gates.test_gate.shutil.which", lambda name: None)

    assert _service({}).resolve_test_runtime("task", None) == "native"


def test_auto_test_runtime_prefers_docker_when_daemon_is_ready(monkeypatch) -> None:
    monkeypatch.setattr("harness.gates.test_gate.shutil.which", lambda name: "/usr/bin/docker")
    config = {"testing": {"runtime": "auto", "docker": {"enabled": True}}}

    assert _service(config, DummyCommandRunner(returncode=0)).resolve_test_runtime("task", None) == "docker"


def test_auto_test_runtime_falls_back_to_native_when_daemon_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("harness.gates.test_gate.shutil.which", lambda name: "/usr/bin/docker")
    config = {"testing": {"runtime": "auto", "docker": {"enabled": True}}}

    assert _service(config, DummyCommandRunner(returncode=1)).resolve_test_runtime("task", None) == "native"


def test_auto_test_runtime_falls_back_to_native_when_docker_disabled() -> None:
    config = {"testing": {"runtime": "auto", "docker": {"enabled": False}}}
    assert _service(config).resolve_test_runtime("task", None) == "native"


def test_explicit_docker_runtime_still_selects_docker(monkeypatch) -> None:
    monkeypatch.setattr("harness.gates.test_gate.shutil.which", lambda name: None)
    config = {"testing": {"runtime": "docker", "docker": {"enabled": True}}}

    assert _service(config).resolve_test_runtime("task", None) == "docker"


def test_repo_digest_ignores_openorchestra_cache(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    service = _service({})
    before = service.repo_content_digest(repo)
    cache_dir = repo / ".openorchestra-cache"
    cache_dir.mkdir()
    (cache_dir / "pip-cache").write_text("generated\n", encoding="utf-8")

    assert service.repo_content_digest(repo) == before


def test_runtime_selection_event_is_prominent(monkeypatch) -> None:
    monkeypatch.setattr("harness.gates.test_gate.shutil.which", lambda name: None)
    events = []
    runtime, diagnostics = _service({}, emit=events.append).resolve_test_runtime_with_diagnostics("task", None)

    service = _service({}, emit=events.append)
    service.emit_runtime_selection("task", 0, "test_gate.md", diagnostics)

    assert runtime == "native"
    assert events
    assert events[-1].event_type == "test_runtime_selected"
    assert "[TEST RUNTIME]" in str(events[-1].message)
    assert "requested=auto" in str(events[-1].message)
    assert "selected=native" in str(events[-1].message)

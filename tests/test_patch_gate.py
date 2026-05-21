from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import harness.patch.gate as patch_gate_module
from harness.adapters.command_runner import CapturedCommandResult
from harness.patch.gate import (
    PatchGatePolicy,
    analyze_unified_diff,
    patch_gate_result_json,
    patch_validation_markdown,
    run_patch_gate,
)
from harness.runtime.spec import RuntimeSpec


def test_patch_gate_accepts_applicable_unified_diff(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("old\n", encoding="utf-8")
    patch = tmp_path / "change.patch"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )

    result = run_patch_gate(
        patch_path=patch,
        source_repo=source,
        materialized_repo_dir=tmp_path / "materialized",
    )

    assert result.status == "pass"
    assert result.apply_check.status == "pass"
    assert result.diff_check.status == "pass"
    assert result.materialized_repo
    assert (result.materialized_repo / "app.py").read_text(encoding="utf-8") == "new\n"
    payload = json.loads(patch_gate_result_json(result, "task-1", 0))
    assert payload["status"] == "pass"
    assert payload["failure_type"] == "none"
    assert payload["next_action"] == "continue"


def test_patch_gate_rejects_non_unified_diff(tmp_path: Path) -> None:
    patch = tmp_path / "bad.patch"
    patch.write_text("change app.py to new\n", encoding="utf-8")

    result = run_patch_gate(
        patch_path=patch,
        source_repo=None,
        materialized_repo_dir=tmp_path / "materialized",
    )

    assert result.status == "fail"
    assert not result.stats.legal_unified_diff
    assert "patch does not contain diff --git file headers" in result.stats.legal_errors
    assert "status: fail" in patch_validation_markdown(result)
    payload = json.loads(patch_gate_result_json(result, "task-1", 0))
    assert payload["failure_type"] == "invalid_unified_diff"
    assert payload["commands"]["apply_check"]["command"] == "git apply --check --whitespace=nowarn <merged_patch.diff>"


def test_patch_gate_result_json_preserves_apply_failure_stderr(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("current\n", encoding="utf-8")
    patch = tmp_path / "stale.patch"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )

    result = run_patch_gate(
        patch_path=patch,
        source_repo=source,
        materialized_repo_dir=tmp_path / "materialized",
    )

    payload = json.loads(patch_gate_result_json(result, "task-1", 1))
    assert payload["status"] == "fail"
    assert payload["failure_type"] == "patch_apply"
    assert payload["round_id"] == 1
    assert payload["commands"]["apply_check"]["exit_code"] != 0
    assert "app.py" in payload["commands"]["apply_check"]["stderr"]
    assert "Patch did not apply" in payload["executor_message"]


def test_patch_gate_passes_runtime_spec_to_git_commands(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("old\n", encoding="utf-8")
    patch = tmp_path / "change.patch"
    patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )
    spec = RuntimeSpec(mode="docker", image="runtime:test", workdir="/workspace")
    calls: list[tuple[list[str], RuntimeSpec | None]] = []

    class FakeRunner:
        def run_capture(self, command, cwd, timeout_seconds=None, input_text=None, env=None, runtime_spec=None):
            calls.append((command, runtime_spec))
            return CapturedCommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(patch_gate_module, "_COMMAND_RUNNER", FakeRunner())

    result = run_patch_gate(
        patch_path=patch,
        source_repo=source,
        materialized_repo_dir=tmp_path / "materialized",
        runtime_spec=spec,
    )

    assert result.status == "pass"
    assert calls
    assert all(call_runtime is spec for _, call_runtime in calls)
    apply_commands = [command for command, _ in calls if command[:2] == ["git", "apply"]]
    assert apply_commands
    assert all(str(patch) not in command for command in apply_commands)
    assert all(command[-1] == ".openorchestra_merged_patch.diff" for command in apply_commands)


def test_patch_gate_accepts_empty_new_file_metadata_diff(tmp_path: Path) -> None:
    patch = tmp_path / "empty-file.patch"
    patch.write_text(
        "diff --git a/tests/__init__.py b/tests/__init__.py\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n",
        encoding="utf-8",
    )

    result = run_patch_gate(
        patch_path=patch,
        source_repo=None,
        materialized_repo_dir=tmp_path / "materialized",
    )

    assert result.status == "pass"
    assert result.stats.legal_unified_diff
    assert result.stats.changed_files == [Path("tests/__init__.py")]
    assert result.stats.changed_line_count == 0
    assert result.materialized_repo
    created = result.materialized_repo / "tests" / "__init__.py"
    assert created.exists()
    assert created.read_text(encoding="utf-8") == ""


def test_patch_gate_accepts_delta_patch_and_exports_cumulative_patch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("one\n", encoding="utf-8")
    round0_patch = tmp_path / "round0.patch"
    round0_patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-one\n"
        "+two\n",
        encoding="utf-8",
    )
    round0 = run_patch_gate(
        patch_path=round0_patch,
        source_repo=source,
        export_base_repo=source,
        materialized_repo_dir=tmp_path / "round0" / "repo",
    )
    assert round0.status == "pass"
    assert round0.materialized_repo

    round1_delta_patch = tmp_path / "round1-delta.patch"
    round1_delta_patch.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-two\n"
        "+three\n",
        encoding="utf-8",
    )
    check_dir = tmp_path / "base-check"
    shutil.copytree(source, check_dir)
    stale_check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", str(round1_delta_patch)],
        cwd=check_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    assert stale_check.returncode != 0

    round1 = run_patch_gate(
        patch_path=round1_delta_patch,
        source_repo=round0.materialized_repo,
        export_base_repo=source,
        materialized_repo_dir=tmp_path / "round1" / "repo",
    )

    assert round1.status == "pass"
    assert round1.materialized_repo
    assert (round1.materialized_repo / "app.py").read_text(encoding="utf-8") == "three\n"
    assert round1.cumulative_patch_path and round1.cumulative_patch_path.exists()
    cumulative = round1.cumulative_patch_path.read_text(encoding="utf-8")
    assert "-one" in cumulative
    assert "+three" in cumulative
    assert "-two" not in cumulative
    assert round1.cumulative_check.status == "pass"


def test_patch_gate_rejects_large_diff_and_many_deletions() -> None:
    patch_text = ""
    for index in range(3):
        patch_text += (
            f"diff --git a/file{index}.txt b/file{index}.txt\n"
            "deleted file mode 100644\n"
            f"--- a/file{index}.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-old\n"
        )

    stats = analyze_unified_diff(
        patch_text,
        PatchGatePolicy(max_changed_lines=2, max_deleted_files=2),
    )

    assert not stats.size_ok
    assert "changed line count 3 exceeds limit 2" in stats.size_errors
    assert "deleted file count 3 exceeds limit 2" in stats.size_errors


def test_patch_gate_rejects_symlink_file_mode() -> None:
    patch_text = (
        "diff --git a/link.txt b/link.txt\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        "+++ b/link.txt\n"
        "@@ -0,0 +1 @@\n"
        "+/tmp/outside\n"
    )

    stats = analyze_unified_diff(patch_text)

    assert not stats.legal_unified_diff
    assert "unsupported patch file mode 120000: only regular file modes 100644 and 100755 are allowed" in stats.legal_errors


def test_patch_gate_rejects_sensitive_token_paths() -> None:
    patch_text = (
        "diff --git a/config/api_token.txt b/config/api_token.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/config/api_token.txt\n"
        "@@ -0,0 +1 @@\n"
        "+secret\n"
    )

    stats = analyze_unified_diff(patch_text)

    assert not stats.scope_ok
    assert "forbidden sensitive token/key-related file path: config/api_token.txt" in stats.scope_errors

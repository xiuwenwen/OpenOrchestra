from __future__ import annotations

from pathlib import Path

from harness.patch.gate import PatchGatePolicy, analyze_unified_diff, run_patch_gate


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

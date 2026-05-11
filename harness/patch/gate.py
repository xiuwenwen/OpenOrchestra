from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


FORBIDDEN_PATCH_TOP_LEVEL_NAMES = {"artifacts", "deliver", "deliveries", "workspaces"}
FORBIDDEN_PATCH_PATH_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
FORBIDDEN_PATCH_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
FORBIDDEN_PATCH_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
SENSITIVE_NAME_PATTERN = re.compile(
    r"(^|[._-])(api[_-]?key|auth|credential|credentials|secret|token)([._-]|$)",
    re.IGNORECASE,
)


CopySourceFn = Callable[[Path, Path], None]


@dataclass(frozen=True)
class PatchGatePolicy:
    max_changed_lines: int = 20_000
    max_deleted_files: int = 50
    forbidden_top_level_names: set[str] = field(default_factory=lambda: set(FORBIDDEN_PATCH_TOP_LEVEL_NAMES))
    forbidden_path_parts: set[str] = field(default_factory=lambda: set(FORBIDDEN_PATCH_PATH_PARTS))
    forbidden_file_names: set[str] = field(default_factory=lambda: set(FORBIDDEN_PATCH_FILE_NAMES))
    forbidden_suffixes: set[str] = field(default_factory=lambda: set(FORBIDDEN_PATCH_SUFFIXES))


@dataclass(frozen=True)
class PatchStats:
    changed_files: list[Path]
    deleted_files: list[Path]
    added_lines: int
    removed_lines: int
    legal_errors: list[str]
    scope_errors: list[str]
    size_errors: list[str]

    @property
    def changed_line_count(self) -> int:
        return self.added_lines + self.removed_lines

    @property
    def legal_unified_diff(self) -> bool:
        return not self.legal_errors

    @property
    def scope_ok(self) -> bool:
        return not self.scope_errors

    @property
    def size_ok(self) -> bool:
        return not self.size_errors


@dataclass(frozen=True)
class CommandResult:
    status: str
    command: str
    exit_code: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PatchGateResult:
    patch_path: Path
    source_repo: Path | None
    materialized_repo: Path | None
    stats: PatchStats
    apply_check: CommandResult
    materialize: CommandResult
    diff_check: CommandResult

    @property
    def status(self) -> str:
        if (
            self.stats.legal_unified_diff
            and self.stats.scope_ok
            and self.stats.size_ok
            and self.apply_check.status == "pass"
            and self.materialize.status == "success"
            and self.diff_check.status == "pass"
        ):
            return "pass"
        return "fail"

    @property
    def precheck_errors(self) -> list[str]:
        return [*self.stats.legal_errors, *self.stats.scope_errors, *self.stats.size_errors]


def run_patch_gate(
    *,
    patch_path: Path,
    source_repo: Path | None,
    materialized_repo_dir: Path,
    policy: PatchGatePolicy | None = None,
    copy_source: CopySourceFn | None = None,
) -> PatchGateResult:
    policy = policy or PatchGatePolicy()
    patch_text = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.exists() else ""
    stats = analyze_unified_diff(patch_text, policy)
    apply_check = _run_apply_check(patch_path, source_repo, copy_source)
    materialized_repo: Path | None = None
    materialize = CommandResult(
        status="skipped",
        command="git apply --whitespace=nowarn <merged_patch.diff>",
        exit_code=None,
        stdout="",
        stderr=_materialization_skip_reason(stats, apply_check),
    )
    diff_check = CommandResult(
        status="skipped",
        command="git diff --check",
        exit_code=None,
        stdout="",
        stderr="Materialization was not run.",
    )
    if stats.legal_unified_diff and stats.scope_ok and stats.size_ok and apply_check.status == "pass":
        materialized_repo, materialize, diff_check = _materialize_and_diff_check(
            patch_path,
            source_repo,
            materialized_repo_dir,
            stats.changed_files,
            copy_source,
        )
    return PatchGateResult(
        patch_path=patch_path,
        source_repo=source_repo,
        materialized_repo=materialized_repo,
        stats=stats,
        apply_check=apply_check,
        materialize=materialize,
        diff_check=diff_check,
    )


def analyze_unified_diff(patch_text: str, policy: PatchGatePolicy | None = None) -> PatchStats:
    policy = policy or PatchGatePolicy()
    changed_files: list[Path] = []
    deleted_files: list[Path] = []
    legal_errors: list[str] = []
    added_lines = 0
    removed_lines = 0
    file_count = 0
    current_has_old_new = False
    current_has_hunk = False
    current_has_metadata_only_change = False
    current_old_path: Path | None = None
    current_new_path: Path | None = None

    def finish_file() -> None:
        nonlocal current_has_old_new, current_has_hunk, current_has_metadata_only_change, current_old_path, current_new_path
        if current_old_path is None and current_new_path is None:
            return
        if current_has_metadata_only_change and not current_has_old_new and not current_has_hunk:
            return
        if not current_has_old_new:
            legal_errors.append("file diff is missing ---/+++ headers")
        if not current_has_hunk:
            legal_errors.append("file diff is missing unified hunk header")

    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            finish_file()
            file_count += 1
            current_has_old_new = False
            current_has_hunk = False
            current_has_metadata_only_change = False
            current_old_path = None
            current_new_path = None
            paths = _git_diff_paths(line)
            if not paths:
                legal_errors.append(f"invalid diff header: {line}")
                continue
            for path in paths:
                if path not in changed_files:
                    changed_files.append(path)
            current_old_path, current_new_path = paths
            continue
        if line.startswith("--- ") and not line.startswith("----"):
            current_has_old_new = True
            current_old_path = _strip_diff_path(line.split(maxsplit=1)[1])
            continue
        if line.startswith("+++ ") and not line.startswith("++++"):
            current_has_old_new = True
            current_new_path = _strip_diff_path(line.split(maxsplit=1)[1])
            if current_new_path == Path("/dev/null") and current_old_path and current_old_path not in deleted_files:
                deleted_files.append(current_old_path)
            continue
        if line.startswith("deleted file mode ") and current_old_path and current_old_path not in deleted_files:
            current_has_metadata_only_change = True
            deleted_files.append(current_old_path)
            continue
        if line.startswith(("new file mode ", "old mode ", "new mode ", "rename from ", "rename to ")):
            current_has_metadata_only_change = True
            continue
        if line.startswith("@@ "):
            current_has_hunk = True
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_lines += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            removed_lines += 1
            continue

    finish_file()
    if not patch_text.strip():
        legal_errors.append("patch is empty")
    if file_count == 0:
        legal_errors.append("patch does not contain diff --git file headers")
    if not changed_files:
        legal_errors.append("patch does not contain any changed files")

    scope_errors: list[str] = []
    for relative_path in changed_files:
        scope_errors.extend(_patch_path_scope_errors(relative_path, policy))

    size_errors: list[str] = []
    changed_line_count = added_lines + removed_lines
    if changed_line_count > policy.max_changed_lines:
        size_errors.append(
            f"changed line count {changed_line_count} exceeds limit {policy.max_changed_lines}"
        )
    if len(deleted_files) > policy.max_deleted_files:
        size_errors.append(f"deleted file count {len(deleted_files)} exceeds limit {policy.max_deleted_files}")

    return PatchStats(
        changed_files=changed_files,
        deleted_files=deleted_files,
        added_lines=added_lines,
        removed_lines=removed_lines,
        legal_errors=legal_errors,
        scope_errors=scope_errors,
        size_errors=size_errors,
    )


def patch_validation_markdown(result: PatchGateResult) -> str:
    return "\n".join(
        [
            "# Patch Validation",
            "",
            f"status: {result.status}",
            f"patch: {result.patch_path}",
            f"source_repo: {result.source_repo or 'none'}",
            f"legal_unified_diff: {str(result.stats.legal_unified_diff).lower()}",
            f"scope_status: {'pass' if result.stats.scope_ok else 'fail'}",
            f"size_status: {'pass' if result.stats.size_ok else 'fail'}",
            f"patch_apply_status: {result.apply_check.status}",
            f"materialize_status: {result.materialize.status}",
            f"diff_check_status: {result.diff_check.status}",
            f"changed_line_count: {result.stats.changed_line_count}",
            f"deleted_file_count: {len(result.stats.deleted_files)}",
            f"command: {result.apply_check.command}",
            f"exit_code: {result.apply_check.exit_code if result.apply_check.exit_code is not None else 'n/a'}",
            "",
            "## stdout",
            "",
            "```text",
            result.apply_check.stdout.strip(),
            "```",
            "",
            "## stderr",
            "",
            "```text",
            result.apply_check.stderr.strip(),
            "```",
            "",
        ]
    )


def materialized_repo_markdown(result: PatchGateResult, task_id: str, round_id: int) -> str:
    return "\n".join(
        [
            "# Materialized Repository",
            "",
            f"status: {result.materialize.status}",
            f"task_id: {task_id}",
            f"round_id: {round_id}",
            f"repo_path: {result.materialized_repo or 'none'}",
            f"patch: {result.patch_path}",
            f"source_repo: {result.source_repo or 'none'}",
            f"diff_check_status: {result.diff_check.status}",
            f"command: {result.materialize.command}",
            f"exit_code: {result.materialize.exit_code if result.materialize.exit_code is not None else 'n/a'}",
            f"diff_check_command: {result.diff_check.command}",
            f"diff_check_exit_code: {result.diff_check.exit_code if result.diff_check.exit_code is not None else 'n/a'}",
            "",
            "## stdout",
            "",
            "```text",
            result.materialize.stdout.strip(),
            "```",
            "",
            "## stderr",
            "",
            "```text",
            result.materialize.stderr.strip(),
            "```",
            "",
            "## diff check stdout",
            "",
            "```text",
            result.diff_check.stdout.strip(),
            "```",
            "",
            "## diff check stderr",
            "",
            "```text",
            result.diff_check.stderr.strip(),
            "```",
            "",
        ]
    )


def objective_gate_markdown(result: PatchGateResult, task_id: str, round_id: int) -> str:
    evidence = {
        "patch_apply_check": result.apply_check.status == "pass",
        "materialize_status": result.materialize.status,
        "diff_check": result.diff_check.status == "pass",
        "legal_unified_diff": result.stats.legal_unified_diff,
        "scope_ok": result.stats.scope_ok,
        "size_ok": result.stats.size_ok,
        "changed_files": [str(path) for path in result.stats.changed_files],
        "deleted_files": [str(path) for path in result.stats.deleted_files],
        "changed_line_count": result.stats.changed_line_count,
        "precheck_errors": result.precheck_errors,
    }
    return "\n".join(
        [
            "# Objective Gate",
            "",
            f"status: {result.status}",
            f"task_id: {task_id}",
            f"round_id: {round_id}",
            f"patch: {result.patch_path}",
            f"source_repo: {result.source_repo or 'none'}",
            f"legal_unified_diff: {str(result.stats.legal_unified_diff).lower()}",
            f"scope_status: {'pass' if result.stats.scope_ok else 'fail'}",
            f"size_status: {'pass' if result.stats.size_ok else 'fail'}",
            f"patch_apply_status: {result.apply_check.status}",
            f"materialize_status: {result.materialize.status}",
            f"diff_check_status: {result.diff_check.status}",
            f"changed_line_count: {result.stats.changed_line_count}",
            f"deleted_file_count: {len(result.stats.deleted_files)}",
            "",
            "## Evidence JSON",
            "",
            "```json",
            _json_dumps(evidence),
            "```",
            "",
            "## Changed Files",
            "",
            *(f"- {path}" for path in result.stats.changed_files),
            "",
            "## Deleted Files",
            "",
            *(f"- {path}" for path in result.stats.deleted_files),
            "",
            "## Gate Errors",
            "",
            *(f"- {error}" for error in result.precheck_errors),
            "",
        ]
    )


def _json_dumps(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _run_apply_check(patch_path: Path, source_repo: Path | None, copy_source: CopySourceFn | None) -> CommandResult:
    command = "git apply --check --whitespace=nowarn <merged_patch.diff>"
    if not shutil.which("git"):
        return CommandResult("skipped", command, None, "", "git executable was not found on PATH.")
    with tempfile.TemporaryDirectory(prefix="harness-patch-check-") as tmp:
        check_dir = Path(tmp) / "repo"
        _prepare_source_tree(source_repo, check_dir, copy_source)
        completed = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)],
            cwd=check_dir,
            text=True,
            capture_output=True,
            check=False,
        )
        return CommandResult(
            "pass" if completed.returncode == 0 else "fail",
            command,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


def _materialize_and_diff_check(
    patch_path: Path,
    source_repo: Path | None,
    repo_dir: Path,
    changed_files: list[Path],
    copy_source: CopySourceFn | None,
) -> tuple[Path | None, CommandResult, CommandResult]:
    materialize_command = "git apply --whitespace=nowarn <merged_patch.diff>"
    diff_check_command = "git diff --check"
    if not shutil.which("git"):
        return (
            None,
            CommandResult("skipped", materialize_command, None, "", "git executable was not found on PATH."),
            CommandResult("skipped", diff_check_command, None, "", "git executable was not found on PATH."),
        )
    tmp_repo_dir = repo_dir.parent / f".repo_tmp_{uuid.uuid4().hex}"
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    _prepare_source_tree(source_repo, tmp_repo_dir, copy_source)
    _initialize_diff_check_repo(tmp_repo_dir)
    completed = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch_path)],
        cwd=tmp_repo_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    materialize = CommandResult(
        "success" if completed.returncode == 0 else "failed",
        materialize_command,
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )
    if completed.returncode != 0:
        shutil.rmtree(tmp_repo_dir, ignore_errors=True)
        return None, materialize, CommandResult("skipped", diff_check_command, None, "", "Materialization failed.")
    _stage_new_files_for_diff_check(tmp_repo_dir, changed_files)
    diff_check_completed = subprocess.run(
        ["git", "diff", "--check"],
        cwd=tmp_repo_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    diff_check = CommandResult(
        "pass" if diff_check_completed.returncode == 0 else "fail",
        diff_check_command,
        diff_check_completed.returncode,
        diff_check_completed.stdout,
        diff_check_completed.stderr,
    )
    if diff_check_completed.returncode != 0:
        shutil.rmtree(tmp_repo_dir, ignore_errors=True)
        failed_materialize = CommandResult(
            "failed",
            materialize.command,
            materialize.exit_code,
            materialize.stdout,
            materialize.stderr,
        )
        return None, failed_materialize, diff_check
    tmp_repo_dir.rename(repo_dir)
    return repo_dir, materialize, diff_check


def _materialization_skip_reason(stats: PatchStats, apply_check: CommandResult) -> str:
    if stats.legal_errors:
        return "Patch is not a legal unified diff; materialization was not run.\n" + "\n".join(stats.legal_errors)
    if stats.scope_errors:
        return "Patch scope gate failed; materialization was not run.\n" + "\n".join(stats.scope_errors)
    if stats.size_errors:
        return "Patch size/delete gate failed; materialization was not run.\n" + "\n".join(stats.size_errors)
    return f"Patch apply-check status was {apply_check.status}; materialization only runs when status is pass."


def _prepare_source_tree(source_repo: Path | None, destination: Path, copy_source: CopySourceFn | None) -> None:
    if source_repo:
        if copy_source:
            copy_source(source_repo, destination)
        else:
            shutil.copytree(source_repo, destination)
    else:
        destination.mkdir(parents=True, exist_ok=True)


def _initialize_diff_check_repo(repo_dir: Path) -> None:
    if not shutil.which("git"):
        return
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, text=True, capture_output=True, check=False)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, text=True, capture_output=True, check=False)


def _stage_new_files_for_diff_check(repo_dir: Path, changed_files: list[Path]) -> None:
    safe_paths = [str(path) for path in changed_files if _is_safe_relative_path(path)]
    if not safe_paths:
        return
    subprocess.run(
        ["git", "add", "-N", "--", *safe_paths],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=False,
    )


def _git_diff_paths(line: str) -> tuple[Path, Path] | None:
    parts = line.split()
    if len(parts) < 4:
        return None
    left = _strip_diff_path(parts[2])
    right = _strip_diff_path(parts[3])
    paths = [path for path in (left, right) if path != Path("/dev/null")]
    if not paths:
        return None
    return paths[0], paths[-1]


def _strip_diff_path(raw_path: str) -> Path:
    path = raw_path.strip().strip('"')
    if path in {"/dev/null", "dev/null"}:
        return Path("/dev/null")
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return Path(path)


def _patch_path_scope_errors(relative_path: Path, policy: PatchGatePolicy) -> list[str]:
    errors: list[str] = []
    if not _is_safe_relative_path(relative_path):
        return [f"unsafe path: {relative_path}"]
    parts = set(relative_path.parts)
    if relative_path.parts and relative_path.parts[0] in policy.forbidden_top_level_names:
        errors.append(f"forbidden generated top-level path: {relative_path}")
    forbidden_parts = sorted(parts & policy.forbidden_path_parts)
    if forbidden_parts:
        errors.append(f"forbidden path component(s) {', '.join(forbidden_parts)} in {relative_path}")
    name = relative_path.name
    if name in policy.forbidden_file_names or name.startswith(".env."):
        errors.append(f"forbidden sensitive file path: {relative_path}")
    if relative_path.suffix in policy.forbidden_suffixes:
        errors.append(f"forbidden sensitive file suffix: {relative_path}")
    if SENSITIVE_NAME_PATTERN.search(name):
        errors.append(f"forbidden sensitive token/key-related file path: {relative_path}")
    return errors


def _is_safe_relative_path(path: Path) -> bool:
    return not path.is_absolute() and ".." not in path.parts

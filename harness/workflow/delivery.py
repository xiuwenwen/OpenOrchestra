from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from harness.agents.result import ArtifactRef
from harness.artifacts.hashing import sha256_file
from harness.artifacts.schemas import required_outputs_for


MATERIALIZED_SUCCESS_MARKER = ".harness_materialized_success.json"


def delivery_required_outputs() -> list[str]:
    return required_outputs_for("communicator", "DELIVERY")


class DeliveryPublisher:
    def __init__(self, orchestrator: Any):
        self.orchestrator = orchestrator

    def publish_delivery(self, task_id: str, final_path: Path) -> Path:
        o = self.orchestrator
        task = o.repository.get_task(task_id)
        prompt = task["user_prompt"] if task else task_id
        deliver_root = Path(o.config["system"].get("deliver_root", "./deliver")).expanduser().resolve()
        project_dir = self.delivery_project_dir(task_id, prompt, deliver_root)
        project_dir.mkdir(parents=True, exist_ok=True)
        destination = project_dir / "final_delivery.md"
        shutil.copy2(final_path, destination)
        usage_guide = o.communicator.latest_usage_guide(task_id)
        if usage_guide and usage_guide.exists():
            shutil.copy2(usage_guide, project_dir / "usage_guide.md")
        copied_artifacts = self.publish_supporting_artifacts(task_id, project_dir)
        source_files = self.publish_materialized_source(task_id, project_dir)
        dependency_files = self.publish_dependency_installer(project_dir)
        success_path = self.write_success_path(task_id, project_dir, destination, usage_guide)
        manifest = project_dir / "artifacts_manifest.md"
        lines = [
            "# Delivery Artifact Manifest",
            "",
            f"task_id: {task_id}",
            f"success_path: {project_dir}",
            f"source_final_delivery: {final_path}",
            f"published_final_delivery: {destination}",
            "",
            "## Published Files",
            "",
            f"- final_delivery.md: {destination}",
            f"- success_path.md: {success_path}",
        ]
        if usage_guide and usage_guide.exists():
            lines.append(f"- usage_guide.md: {project_dir / 'usage_guide.md'}")
        if (project_dir / "patches" / "final.patch").exists():
            lines.append(f"- patches/final.patch: {project_dir / 'patches' / 'final.patch'}")
        if source_files:
            lines.append(f"- source/: {project_dir / 'source'}")
        for dependency_file in dependency_files:
            lines.append(f"- {dependency_file.relative_to(project_dir)}: {dependency_file}")
        if copied_artifacts:
            lines.extend(["", "## Supporting Artifacts", ""])
            for artifact_type, path in copied_artifacts:
                lines.append(f"- {artifact_type}: {path}")
        if source_files:
            lines.extend(["", "## Materialized Source Files", ""])
            for path in source_files:
                lines.append(f"- {path.relative_to(project_dir)}")
        else:
            lines.extend(
                [
                    "",
                    "## Materialized Source Files",
                    "",
                    "- none: no safely materializable new-file patch was found. Use `patches/final.patch` with the target repository.",
                ]
            )
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.record_published_artifact(task_id, "success_path.md", success_path)
        self.record_published_artifact(task_id, "artifacts_manifest.md", manifest)
        for dependency_file in dependency_files:
            self.record_published_artifact(task_id, dependency_file.name, dependency_file)
        return destination

    def delivery_success_path(self, task_id: str) -> Path | None:
        o = self.orchestrator
        task = o.repository.get_task(task_id)
        if not task:
            return None
        deliver_root = Path(o.config["system"].get("deliver_root", "./deliver")).expanduser().resolve()
        project_dir = self.delivery_project_dir(task_id, task["user_prompt"], deliver_root)
        return project_dir if project_dir.exists() else None

    def delivery_project_dir(self, task_id: str, prompt: str, deliver_root: Path | None = None) -> Path:
        root = deliver_root or Path(self.orchestrator.config["system"].get("deliver_root", "./deliver")).expanduser().resolve()
        return root / f"{self.slugify_project_name(prompt)}-{task_id[:8]}"

    def write_success_path(self, task_id: str, project_dir: Path, final_delivery: Path, usage_guide: Path | None) -> Path:
        path = project_dir / "success_path.md"
        lines = [
            "# Success Path",
            "",
            f"task_id: {task_id}",
            f"success_path: {project_dir}",
            f"final_delivery: {final_delivery}",
        ]
        if usage_guide and usage_guide.exists():
            lines.append(f"usage_guide: {project_dir / 'usage_guide.md'}")
        lines.append(f"artifacts_manifest: {project_dir / 'artifacts_manifest.md'}")
        lines.extend(
            [
                "",
                "Open this directory to inspect the delivered result and supporting artifacts.",
                "If the Web viewer is running, select the same task_id to inspect role rounds and role artifacts.",
            ]
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def record_published_artifact(self, task_id: str, artifact_type: str, path: Path) -> None:
        o = self.orchestrator
        if not path.exists() or not path.is_file():
            return

        def build_ref(version: int) -> ArtifactRef:
            return ArtifactRef(
                artifact_id=f"published-{task_id}-{artifact_type}-{version}",
                task_id=task_id,
                phase_id=None,
                role="orchestrator",
                agent_id="harness",
                artifact_type=artifact_type,
                path=path,
                version=version,
                hash=sha256_file(path),
            )

        o.repository.create_artifact_with_next_version(
            task_id,
            artifact_type,
            build_ref,
        )

    def publish_supporting_artifacts(self, task_id: str, project_dir: Path) -> list[tuple[str, Path]]:
        delivery_config = self.orchestrator.config.get("delivery", {})
        include_internal_artifacts = (
            bool(delivery_config.get("include_internal_artifacts", False))
            if isinstance(delivery_config, dict)
            else False
        )
        artifact_types = (
            [
                "merged_patch_metadata.md",
                "changed_files.md",
                "self_check.md",
                "fix_notes.md",
                "review_report.md",
            ]
            if include_internal_artifacts
            else []
        )
        copied: list[tuple[str, Path]] = []
        artifact_dir = project_dir / "artifacts"
        patch_dir = project_dir / "patches"
        for artifact_type in artifact_types:
            artifacts = self.orchestrator.repository.list_artifacts(task_id, artifact_type)
            if not artifacts:
                continue
            source = Path(artifacts[-1]["path"])
            if not source.exists():
                continue
            destination = artifact_dir / self.safe_deliver_filename(artifact_type)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append((artifact_type, destination))
        final_patch_ref = self.latest_patch_artifact(task_id)
        if final_patch_ref:
            source = Path(final_patch_ref["path"])
            if source.exists():
                patch_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, patch_dir / "final.patch")
        return copied

    def publish_materialized_source(self, task_id: str, project_dir: Path) -> list[Path]:
        o = self.orchestrator
        patch_path = project_dir / "patches" / "final.patch"
        source_dir = project_dir / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        materialized_repo = o._latest_materialized_repo(task_id)
        if materialized_repo:
            shutil.copytree(materialized_repo, source_dir, ignore=self.copy_ignore_for_publish)
            return sorted(path for path in source_dir.rglob("*") if path.is_file())
        if not patch_path.exists():
            return []
        files = self.materialized_files_from_unified_diff(
            patch_path.read_text(encoding="utf-8", errors="replace"),
            o._source_repo_for_existing_project_task(task_id),
            include_modified=True,
        )
        if not files:
            return []
        written: list[Path] = []
        for relative_name, lines in sorted(files.items()):
            if not self.is_safe_relative_path(relative_name):
                continue
            destination = source_dir / relative_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written.append(destination)
        return written

    def publish_dependency_installer(self, project_dir: Path) -> list[Path]:
        source_dir = project_dir / "source" if (project_dir / "source").is_dir() else project_dir
        if not source_dir.exists():
            return []
        written: list[Path] = []
        dependency_file = next(
            (path for path in (source_dir / "requirements.txt", source_dir / "request.txt") if path.exists()),
            None,
        )
        install_command = ""
        if (source_dir / "pyproject.toml").exists():
            install_command = '.venv/bin/python -m pip install -e ".[dev]"'
        if dependency_file is None:
            inferred_dependencies = self.infer_delivery_python_dependencies(source_dir, project_dir)
            if inferred_dependencies:
                dependency_file = source_dir / "requirements.txt"
                dependency_file.write_text("\n".join(inferred_dependencies) + "\n", encoding="utf-8")
                written.append(dependency_file)
        if not install_command and dependency_file is not None:
            install_command = f".venv/bin/python -m pip install -r {dependency_file.name}"
        if not install_command:
            return []
        installer = source_dir / "install_dependencies.sh"
        installer.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'cd "$(dirname "$0")"',
                    'PYTHON_BIN="${PYTHON_BIN:-python3}"',
                    'if [ ! -d ".venv" ]; then',
                    '  "$PYTHON_BIN" -m venv .venv',
                    "fi",
                    ".venv/bin/python -m pip install --upgrade pip",
                    install_command,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        installer.chmod(0o755)
        written.append(installer)
        return written

    def infer_delivery_python_dependencies(self, source_dir: Path, project_dir: Path) -> list[str]:
        if not any(source_dir.rglob("*.py")):
            return []
        text_parts: list[str] = []
        for path in (
            project_dir / "usage_guide.md",
            project_dir / "final_delivery.md",
            source_dir / "README.md",
            source_dir / "readme.md",
        ):
            if path.exists() and path.is_file():
                text_parts.append(path.read_text(encoding="utf-8", errors="replace").lower())
        text = "\n".join(text_parts)
        dependencies: list[str] = []
        has_pytest_signal = (
            (source_dir / "tests").exists()
            or "python -m pytest" in text
            or "python3 -m pytest" in text
            or re.search(r"(^|\s)pytest(\s|$)", text) is not None
        )
        if has_pytest_signal:
            dependencies.append("pytest")
        if "--cov" in text or "pytest-cov" in text:
            dependencies.append("pytest-cov")
        return dependencies

    def copy_ignore_for_publish(self, directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", MATERIALIZED_SUCCESS_MARKER}
        }

    def new_files_from_unified_diff(self, patch_text: str) -> dict[Path, list[str]]:
        return self.materialized_files_from_unified_diff(patch_text, source_repo=None, include_modified=False)

    def materialized_files_from_unified_diff(
        self,
        patch_text: str,
        source_repo: Path | None,
        *,
        include_modified: bool,
    ) -> dict[Path, list[str]]:
        files: dict[Path, list[str]] = {}
        current_path: Path | None = None
        old_path: Path | None = None
        current_lines: list[str] = []
        current_is_new_file = False
        current_is_deleted_file = False
        base_lines: list[str] = []
        cursor = 0
        in_hunk = False

        def flush() -> None:
            nonlocal current_path, old_path, current_lines, current_is_new_file, current_is_deleted_file, base_lines, cursor, in_hunk
            if current_path and not current_is_deleted_file and (
                current_is_new_file or (include_modified and (base_lines or current_lines))
            ):
                if not current_is_new_file and base_lines:
                    current_lines.extend(base_lines[cursor:])
                files[current_path] = current_lines
            current_path = None
            old_path = None
            current_lines = []
            current_is_new_file = False
            current_is_deleted_file = False
            base_lines = []
            cursor = 0
            in_hunk = False

        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                flush()
                continue
            if line == "--- /dev/null":
                current_is_new_file = True
                continue
            if line.startswith("--- "):
                target = self.strip_diff_path(line[4:].strip())
                if target != Path("/dev/null"):
                    old_path = target
                continue
            if line.startswith("+++ "):
                target = self.strip_diff_path(line[4:].strip())
                if target == Path("/dev/null"):
                    current_is_deleted_file = True
                    continue
                current_path = target
                if not current_is_new_file:
                    base_path = old_path or current_path
                    if source_repo and self.is_safe_relative_path(base_path):
                        source_file = source_repo / base_path
                        if source_file.exists() and source_file.is_file():
                            base_lines = source_file.read_text(encoding="utf-8", errors="replace").splitlines()
                continue
            if line.startswith("@@"):
                hunk_start = self.parse_old_hunk_start(line)
                if hunk_start is not None and base_lines and not current_is_new_file:
                    target_index = max(0, hunk_start - 1)
                    current_lines.extend(base_lines[cursor:target_index])
                    cursor = target_index
                in_hunk = True
                continue
            if in_hunk and current_path:
                if line.startswith("\\ No newline at end of file"):
                    continue
                if current_is_new_file:
                    if line.startswith("+") and not line.startswith("+++"):
                        current_lines.append(line[1:])
                    continue
                if line.startswith(" ") and base_lines:
                    current_lines.append(line[1:])
                    cursor += 1
                elif line.startswith("-") and not line.startswith("---"):
                    cursor += 1
                elif line.startswith("+") and not line.startswith("+++"):
                    current_lines.append(line[1:])
        flush()
        return files

    def strip_diff_path(self, raw_path: str) -> Path:
        if raw_path == "/dev/null":
            return Path("/dev/null")
        path = raw_path.split("\t", 1)[0].split(" ", 1)[0]
        if path.startswith(("a/", "b/")):
            path = path[2:]
        return Path(path)

    def parse_old_hunk_start(self, header: str) -> int | None:
        match = re.search(r"@@ -(\d+)", header)
        return int(match.group(1)) if match else None

    def latest_patch_artifact(self, task_id: str) -> dict[str, Any] | None:
        for artifact_type in ("merged_patch.diff", "fix_patch.diff", "patch.diff"):
            artifacts = self.orchestrator.repository.list_artifacts(task_id, artifact_type)
            if artifacts:
                return artifacts[-1]
        return None

    def safe_deliver_filename(self, artifact_type: str) -> str:
        return artifact_type.replace("/", "__").replace("\\", "__").replace(" ", "_")

    def is_safe_relative_path(self, path: Path) -> bool:
        return not path.is_absolute() and ".." not in path.parts

    def slugify_project_name(self, prompt: str) -> str:
        ascii_prompt = prompt.encode("ascii", "ignore").decode("ascii").lower()
        compact = re.sub(r"[^a-z0-9]+", "-", ascii_prompt).strip("-")
        compact = re.sub(r"-+", "-", compact)[:32].strip("-")
        return compact or "project"

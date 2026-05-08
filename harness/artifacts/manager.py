from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path

from harness.agents.result import ArtifactRef
from harness.artifacts.hashing import sha256_file
from harness.state.repository import StateRepository


class ArtifactManager:
    def __init__(self, artifact_root: str | Path, repository: StateRepository):
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.repository = repository
        self._lock = threading.RLock()
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def collect_output_dir(self, task_id: str, phase_id: str, role: str, agent_id: str, output_dir: Path) -> list[ArtifactRef]:
        refs: list[ArtifactRef] = []
        for source in sorted(path for path in output_dir.rglob("*") if path.is_file()):
            with self._lock:
                relative = source.relative_to(output_dir)
                artifact_type = relative.as_posix()
                artifact_id = str(uuid.uuid4())

                def build_ref(version: int) -> ArtifactRef:
                    destination = (
                        self.artifact_root
                        / task_id
                        / phase_id
                        / role
                        / agent_id
                        / artifact_type
                        / f"v{version}"
                        / source.name
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        raise FileExistsError(f"Artifact destination already exists: {destination}")
                    shutil.copy2(source, destination)
                    return ArtifactRef(
                        artifact_id=artifact_id,
                        task_id=task_id,
                        phase_id=phase_id,
                        role=role,
                        agent_id=agent_id,
                        artifact_type=artifact_type,
                        path=destination,
                        version=version,
                        hash=sha256_file(destination),
                    )

                ref = self.repository.create_artifact_with_next_version(
                    task_id,
                    artifact_type,
                    build_ref,
                )
                refs.append(ref)
        return refs

    def create_text_artifact(
        self,
        task_id: str,
        artifact_type: str,
        content: str,
        phase_id: str | None = None,
        role: str | None = "context",
        agent_id: str | None = "harness",
    ) -> ArtifactRef:
        with self._lock:
            artifact_id = str(uuid.uuid4())

            def build_ref(version: int) -> ArtifactRef:
                destination = self.artifact_root / task_id / "context" / artifact_type / f"v{version}" / Path(artifact_type).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(f"Artifact destination already exists: {destination}")
                destination.write_text(content, encoding="utf-8")
                return ArtifactRef(
                    artifact_id=artifact_id,
                    task_id=task_id,
                    phase_id=phase_id,
                    role=role,
                    agent_id=agent_id,
                    artifact_type=artifact_type,
                    path=destination,
                    version=version,
                    hash=sha256_file(destination),
                )

            return self.repository.create_artifact_with_next_version(
                task_id,
                artifact_type,
                build_ref,
            )

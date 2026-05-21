from __future__ import annotations

from pathlib import Path


def test_v2_architecture_doc_declares_bounded_contexts() -> None:
    content = Path("docs/harness-v2-architecture.md").read_text(encoding="utf-8")

    for phrase in (
        "Control Plane",
        "Execution Plane",
        "Artifact Plane",
        "Materialization Plane",
        "Gate Plane",
        "Event Store",
        "Observability Plane",
        "raw evidence is immutable",
        "workflow decisions are replayable",
    ):
        assert phrase in content


def test_v2_packages_exist() -> None:
    for package in ("domain", "events", "artifact_plane"):
        assert (Path("harness") / package / "__init__.py").is_file()

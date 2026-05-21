from __future__ import annotations

from harness.contracts.role_boundaries import ROLE_BOUNDARIES, role_boundary_prompt_lines_for


def test_role_boundaries_define_core_role_ownership() -> None:
    assert ROLE_BOUNDARIES["executor"].source_access.startswith("writable")
    assert "does not own dependency/setup failure classification" in ROLE_BOUNDARIES["executor"].environment_authority
    assert "owns environment repair loop" in ROLE_BOUNDARIES["tester"].environment_authority
    assert "read-only" in ROLE_BOUNDARIES["reviewer"].source_access


def test_role_boundary_prompt_lines_are_machine_stable() -> None:
    lines = role_boundary_prompt_lines_for("tester")

    assert lines[0].startswith("- Responsibility:")
    assert any("Source access:" in line for line in lines)
    assert any("Environment authority:" in line and "environment repair loop" in line for line in lines)

from __future__ import annotations

from harness.cli.commands import bare_command_line, command_description, matching_commands, resolve_command


def test_bare_command_line_accepts_only_known_short_commands() -> None:
    assert bare_command_line("continue") == "/continue"
    assert bare_command_line("resume 2") == "/resume 2"
    assert bare_command_line("continue fixing the project") is None
    assert bare_command_line("explain this failure") is None


def test_command_aliases_and_descriptions_are_resolved_centrally() -> None:
    assert resolve_command("/h") == "/help"
    assert resolve_command("/go") == "/goal"
    assert matching_commands("/go") == ["/goal"]
    assert command_description("/h").startswith("-> /help")

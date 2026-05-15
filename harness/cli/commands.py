from __future__ import annotations


COMMANDS = {
    "/backend": "Show current backend",
    "/use": "Switch underlying agent backend",
    "/history": "List recent tasks",
    "/resume": "Use a historical task as context",
    "/continue": "Continue/retry the active historical task",
    "/clean": "Remove intermediate files for the selected task",
    "/diagnose": "Export a diagnostics bundle for a task",
    "/goal": "Set test/fix max rounds to 10",
    "/current": "Show selected historical context",
    "/clear": "Clear selected historical context",
    "/ui": "Start or show the local Web execution viewer",
    "/help": "Show command help",
    "/exit": "Quit",
    "/quit": "Quit",
}
COMMAND_ALIASES = {
    "/h": "/help",
    "/?": "/help",
    "/tasks": "/history",
    "/select": "/resume",
    "/task": "/resume",
    "/switch": "/use",
    "/retry": "/continue",
    "/run": "/continue",
}
BARE_COMMAND_ALIASES = {
    "help": "/help",
    "history": "/history",
    "tasks": "/history",
    "resume": "/resume",
    "select": "/resume",
    "task": "/resume",
    "continue": "/continue",
    "retry": "/continue",
    "run": "/continue",
    "clean": "/clean",
    "diagnose": "/diagnose",
    "goal": "/goal",
    "current": "/current",
    "ui": "/ui",
}
BARE_COMMANDS_WITH_ARGS = {"history", "tasks", "resume", "select", "task", "diagnose"}


def bare_command_line(text: str) -> str | None:
    parts = text.split()
    if not parts or parts[0].startswith("/"):
        return None
    token = parts[0].lower()
    command = BARE_COMMAND_ALIASES.get(token)
    if not command:
        return None
    if len(parts) > 1 and token not in BARE_COMMANDS_WITH_ARGS:
        return None
    return " ".join([command, *parts[1:]])


def resolve_command(token: str) -> str:
    if token in COMMAND_ALIASES:
        return COMMAND_ALIASES[token]
    if token in COMMANDS:
        return token
    matches = [COMMAND_ALIASES.get(match, match) for match in matching_commands(token)]
    unique_matches = sorted(set(matches))
    if len(unique_matches) == 1:
        return unique_matches[0]
    return token


def matching_commands(prefix: str) -> list[str]:
    candidates = sorted([*COMMANDS.keys(), *COMMAND_ALIASES.keys()])
    return [command for command in candidates if command.startswith(prefix)]


def command_description(command: str) -> str:
    canonical = COMMAND_ALIASES.get(command, command)
    description = COMMANDS.get(canonical, "")
    alias_note = f" -> {canonical}" if command != canonical else ""
    return f"{alias_note} {description}".strip()

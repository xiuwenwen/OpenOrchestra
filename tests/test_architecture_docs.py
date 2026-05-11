from __future__ import annotations

from pathlib import Path


def test_readme_workflow_matches_current_review_flow() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "review judgement" not in readme.lower()
    assert "REVIEWING / REVIEW_JUDGEMENT / REVIEW_FIXING loop" not in readme
    assert "REVIEWING / REVIEW_FIXING / REGRESSION_TESTING loop" in readme


def test_architecture_flow_does_not_document_removed_final_judgement_path() -> None:
    architecture = Path("system_architecture_and_flow.md").read_text(encoding="utf-8")

    assert "REVIEW_JUDGEMENT --> FINAL_JUDGEMENT" not in architecture
    assert "FINAL_JUDGEMENT --> DELIVERY" not in architecture
    assert "REVIEWING --> DELIVERY: Reviewer approves with runtime-ready verdict" in architecture


def test_readme_delivery_contract_documents_json_envelope() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "It is a JSON role return envelope" in readme
    assert '"return_code": 0' in readme
    assert "Do not copy those verdict codes into `return_code` or `artifact_result_code`." in readme


def test_delivery_handoff_logic_is_not_embedded_in_main_entrypoint() -> None:
    text = Path("harness/main.py").read_text(encoding="utf-8")

    assert "from harness.delivery.handoff import" in text
    assert "def build_delivery_handoff" not in text
    assert "def _delivery_run_commands" not in text


def test_terminal_dashboard_logic_is_not_embedded_in_main_entrypoint() -> None:
    text = Path("harness/main.py").read_text(encoding="utf-8")

    assert "from harness.ui.terminal_dashboard import" in text
    assert "class DashboardProgressReporter" not in text
    assert "class ConsoleProgressReporter" not in text


def test_user_env_logic_is_not_embedded_in_main_entrypoint() -> None:
    text = Path("harness/main.py").read_text(encoding="utf-8")

    assert "from harness.config.user_env import" in text
    assert "def load_user_env" not in text
    assert "ENV_CONFIG_SPECS: dict" not in text


def test_cli_command_registry_is_not_embedded_in_main_entrypoint() -> None:
    text = Path("harness/main.py").read_text(encoding="utf-8")
    command_registry = Path("harness/cli/commands.py").read_text(encoding="utf-8")

    assert "from harness.cli.interactive import InteractiveCLI" in text
    assert "COMMANDS = {" not in text
    assert "BARE_COMMAND_ALIASES =" not in text
    assert "COMMANDS = {" in command_registry


def test_interactive_cli_logic_is_not_embedded_in_main_entrypoint() -> None:
    text = Path("harness/main.py").read_text(encoding="utf-8")

    assert len(text.splitlines()) < 300
    assert "class InteractiveCLI" not in text
    assert "class HarnessCompleter" not in text
    assert "def run_once" not in text
    assert "def start_ui_server" not in text


def test_regression_rounds_do_not_use_stride_encoding() -> None:
    workflow_engine = Path("harness/workflow/engine.py").read_text(encoding="utf-8")
    migrations = Path("harness/state/migrations.sql").read_text(encoding="utf-8")

    assert "REGRESSION_ROUND_STRIDE" not in workflow_engine
    assert "* 1000" not in workflow_engine
    assert "loop_type TEXT" in migrations
    assert "parent_round_id INTEGER" in migrations
    assert "iteration_id INTEGER" in migrations

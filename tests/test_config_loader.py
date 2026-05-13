from __future__ import annotations

from harness.config.loader import dump_config, load_config


def test_config_loader_reads_inline_and_block_lists(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "testing:",
                "  commands:",
                '    - "python -m pytest -q"',
                "    - npm test",
                "  setup_commands: []",
                "runtime_readiness:",
                '  commands: ["python -m compileall -q ."]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config["testing"]["commands"] == ["python -m pytest -q", "npm test"]
    assert config["testing"]["setup_commands"] == []
    assert config["runtime_readiness"]["commands"] == ["python -m compileall -q ."]


def test_config_dump_round_trips_lists(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config = {"testing": {"commands": ["python -m pytest -q"], "setup_commands": []}}

    config_path.write_text(dump_config(config), encoding="utf-8")

    assert load_config(config_path) == config

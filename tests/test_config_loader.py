from __future__ import annotations

from harness.config.loader import dump_config, load_config


def test_config_loader_reads_inline_and_block_lists(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "runtime_readiness:",
                '  commands: ["python -m compileall -q ."]',
                "  extra_commands:",
                '    - "python -m pytest -q"',
                "    - npm test",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config["runtime_readiness"]["commands"] == ["python -m compileall -q ."]
    assert config["runtime_readiness"]["extra_commands"] == ["python -m pytest -q", "npm test"]


def test_config_dump_round_trips_lists(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config = {"runtime_readiness": {"commands": ["python -m pytest -q"]}}

    config_path.write_text(dump_config(config), encoding="utf-8")

    assert load_config(config_path) == config

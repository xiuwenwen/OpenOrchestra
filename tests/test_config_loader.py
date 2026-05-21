from __future__ import annotations

import pytest

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


def test_config_loader_reads_list_of_mappings(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "runtime:",
                "  docker:",
                "    backend_config_mounts:",
                "      claude:",
                "        - host: ~/.claude",
                "          container: /home/openorchestra/.claude",
                "          read_only: true",
                "          writable_subpaths: [\"session-env\"]",
                "        - host: ~/.claude.json",
                "          container: /home/openorchestra/.claude.json",
                "          read_only: true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    mounts = config["runtime"]["docker"]["backend_config_mounts"]["claude"]
    assert mounts == [
        {
            "host": "~/.claude",
            "container": "/home/openorchestra/.claude",
            "read_only": True,
            "writable_subpaths": ["session-env"],
        },
        {"host": "~/.claude.json", "container": "/home/openorchestra/.claude.json", "read_only": True},
    ]


def test_config_dump_round_trips_lists(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config = {
        "runtime_readiness": {"commands": ["python -m pytest -q"]},
        "runtime": {
            "docker": {
                "backend_config_mounts": {
                    "claude": [
                        {"host": "~/.claude", "container": "/home/openorchestra/.claude", "read_only": True}
                    ]
                }
            }
        },
    }

    config_path.write_text(dump_config(config), encoding="utf-8")

    assert load_config(config_path) == config


def test_config_loader_accepts_four_space_indentation_and_inline_comments(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "runtime_readiness:",
                "    commands:",
                '        - "pytest # not a comment" # real comment',
                "    enabled: true # inline comment",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config["runtime_readiness"]["commands"] == ["pytest # not a comment"]
    assert config["runtime_readiness"]["enabled"] is True


def test_config_loader_reports_path_and_line_for_unsupported_yaml(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime_readiness:\n  notes: |\n    unsupported multiline\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert str(config_path) in message
    assert "line 2" in message
    assert "multiline YAML scalars are not supported" in message

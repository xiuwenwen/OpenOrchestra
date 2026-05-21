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

from __future__ import annotations


NONE = "none"
SOURCE_BUG = "source_bug"
ENVIRONMENT_BUG = "environment_bug"
ENV_SETUP = "env_setup"
TEST_COMMAND = "test_command"
TEST_COMMAND_BUG = "test_command_bug"
CONTRACT_BUG = "contract_bug"
CONTRACT_INVALID = "contract_invalid"
PROCESS_BUG = "process_bug"
AGENT_RUNTIME = "agent_runtime"
INFRA = "infra"
PATCH_APPLY = "patch_apply"
TEST = "test"
INCONCLUSIVE = "inconclusive"


FAILURE_TYPES = {
    NONE,
    SOURCE_BUG,
    ENVIRONMENT_BUG,
    ENV_SETUP,
    TEST_COMMAND,
    TEST_COMMAND_BUG,
    CONTRACT_BUG,
    CONTRACT_INVALID,
    PROCESS_BUG,
    AGENT_RUNTIME,
    INFRA,
    PATCH_APPLY,
    TEST,
    INCONCLUSIVE,
}

RUNTIME_BLOCKER_FAILURE_TYPES = {
    ENVIRONMENT_BUG,
    ENV_SETUP,
    TEST_COMMAND,
    TEST_COMMAND_BUG,
    CONTRACT_BUG,
    CONTRACT_INVALID,
    PROCESS_BUG,
    AGENT_RUNTIME,
    INFRA,
}

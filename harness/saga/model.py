from __future__ import annotations

from dataclasses import dataclass

from harness.domain import RouteAction


TERMINAL_TARGETS = {"complete", "fail", "block"}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    retryable_events: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


@dataclass(frozen=True)
class SagaRoute:
    on_event: str
    target_step: str
    action: RouteAction

    def __post_init__(self) -> None:
        if not self.on_event:
            raise ValueError("on_event is required")
        if not self.target_step:
            raise ValueError("target_step is required")


@dataclass(frozen=True)
class SagaStep:
    name: str
    command_type: str
    expected_events: tuple[str, ...]
    timeout_seconds: int
    retry_policy: RetryPolicy
    routes: tuple[SagaRoute, ...]
    compensation_command: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("step name is required")
        if not self.command_type:
            raise ValueError("command_type is required")
        if not self.expected_events:
            raise ValueError("expected_events is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class SagaDefinition:
    name: str
    steps: tuple[SagaStep, ...]
    initial_step: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("saga name is required")
        step_names = [step.name for step in self.steps]
        if len(step_names) != len(set(step_names)):
            raise ValueError("saga step names must be unique")
        if self.initial_step not in step_names:
            raise ValueError("initial_step must reference a saga step")
        known_targets = set(step_names) | TERMINAL_TARGETS
        for step in self.steps:
            for route in step.routes:
                if route.target_step not in known_targets:
                    raise ValueError(f"route target not found: {route.target_step}")

    def step(self, name: str) -> SagaStep:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(name)


def build_bugfix_v2_saga() -> SagaDefinition:
    return SagaDefinition(
        name="bugfix-v2",
        initial_step="plan",
        steps=(
            SagaStep(
                name="plan",
                command_type="RunPlanner",
                expected_events=("DecisionAccepted",),
                timeout_seconds=900,
                retry_policy=RetryPolicy(max_attempts=2, retryable_events=("ArtifactRejected",)),
                routes=(SagaRoute("DecisionAccepted", "execute_patch", RouteAction.CONTINUE),),
            ),
            SagaStep(
                name="execute_patch",
                command_type="RunExecutor",
                expected_events=("RawArtifactStored", "ArtifactCanonicalized"),
                timeout_seconds=1200,
                retry_policy=RetryPolicy(max_attempts=2, retryable_events=("ArtifactRejected",)),
                routes=(SagaRoute("ArtifactCanonicalized", "materialize", RouteAction.CONTINUE),),
            ),
            SagaStep(
                name="materialize",
                command_type="CreateRepoSnapshot",
                expected_events=("RepoSnapshotCreated",),
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_attempts=1),
                routes=(
                    SagaRoute("SnapshotChanged", "tester_verify", RouteAction.CONTINUE),
                    SagaRoute("SnapshotUnchangedButRetestable", "tester_verify", RouteAction.RETEST_CURRENT_REPO_SNAPSHOT),
                    SagaRoute("SnapshotInvalid", "execute_patch", RouteAction.FIX_SOURCE),
                ),
            ),
            SagaStep(
                name="tester_verify",
                command_type="RunTesterGate",
                expected_events=("GatePassed", "GateFailed", "DecisionAccepted", "DecisionRejected"),
                timeout_seconds=1800,
                retry_policy=RetryPolicy(max_attempts=2, retryable_events=("ArtifactRejected",)),
                routes=(
                    SagaRoute("GatePassed", "review", RouteAction.CONTINUE),
                    SagaRoute("ContractChanged", "tester_verify", RouteAction.RETEST_CURRENT_REPO_SNAPSHOT),
                    SagaRoute("SourceBugDetected", "execute_patch", RouteAction.FIX_SOURCE),
                    SagaRoute("EnvironmentBlocked", "block", RouteAction.BLOCK_TASK),
                ),
            ),
            SagaStep(
                name="review",
                command_type="RunReview",
                expected_events=("DecisionAccepted", "DecisionRejected"),
                timeout_seconds=900,
                retry_policy=RetryPolicy(max_attempts=2, retryable_events=("ArtifactRejected",)),
                routes=(
                    SagaRoute("DecisionAccepted", "final_validation", RouteAction.CONTINUE),
                    SagaRoute("DecisionRejected", "execute_patch", RouteAction.FIX_SOURCE),
                ),
            ),
            SagaStep(
                name="final_validation",
                command_type="RunFinalValidationGate",
                expected_events=("GatePassed", "GateFailed"),
                timeout_seconds=3600,
                retry_policy=RetryPolicy(max_attempts=1),
                routes=(
                    SagaRoute("GatePassed", "delivery", RouteAction.CONTINUE),
                    SagaRoute("GateFailed", "execute_patch", RouteAction.FIX_SOURCE),
                ),
            ),
            SagaStep(
                name="delivery",
                command_type="PublishDelivery",
                expected_events=("TaskCompleted",),
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_attempts=1),
                routes=(SagaRoute("TaskCompleted", "complete", RouteAction.CONTINUE),),
            ),
        ),
    )

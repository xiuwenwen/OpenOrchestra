from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.adapters.health import BackendHealthMonitor
from harness.agents.runner import AgentPhaseRunner
from harness.artifacts.manager import ArtifactManager
from harness.artifacts.validator import ArtifactValidator
from harness.artifacts.visibility import ArtifactVisibilityPolicy
from harness.communication.communicator import Communicator
from harness.config.runtime import RuntimeConfigService
from harness.context.staging import InputStagingService
from harness.core.scheduler import BackendBulkheadScheduler
from harness.gates.patch_gate import PatchGateService
from harness.gates.test_gate import TestGateService
from harness.judge.judge_runner import MockJudge
from harness.logs.logger import get_logger
from harness.materialization.service import MaterializedRepoService
from harness.prompts.builder import PromptBuilder
from harness.state.db import StateDB
from harness.state.repository import StateRepository
from harness.workflow.delivery import DeliveryPublisher
from harness.workflow.engine import WorkflowEngine
from harness.workspace.manager import WorkspaceManager


@dataclass(frozen=True)
class ApplicationServices:
    repository: StateRepository
    config_service: RuntimeConfigService
    workspace_manager: WorkspaceManager
    artifact_manager: ArtifactManager
    artifact_visibility: ArtifactVisibilityPolicy
    validator: ArtifactValidator
    communicator: Communicator
    judge: MockJudge
    backend_health: BackendHealthMonitor
    scheduler: BackendBulkheadScheduler
    prompt_builder: PromptBuilder
    materialized_repo_service: MaterializedRepoService
    test_gate_service: TestGateService
    patch_gate_service: PatchGateService
    input_staging_service: InputStagingService
    agent_runner: AgentPhaseRunner
    delivery_publisher: DeliveryPublisher
    workflow_engine: WorkflowEngine
    logger: Any


def build_orchestrator_services(
    orchestrator: Any,
    *,
    config: dict[str, Any],
    repository: StateRepository | None = None,
    workspace_manager: WorkspaceManager | None = None,
    artifact_manager: ArtifactManager | None = None,
) -> ApplicationServices:
    system = config["system"]
    repository = repository or StateRepository(StateDB(system["state_db"]))
    config_service = RuntimeConfigService(config, repository)
    workspace_manager = workspace_manager or WorkspaceManager(system["workspace_root"])
    artifact_manager = artifact_manager or ArtifactManager(system["artifact_root"], repository)
    artifact_visibility = ArtifactVisibilityPolicy()
    validator = ArtifactValidator()
    communicator = Communicator(repository)
    judge = MockJudge()
    backend_health = BackendHealthMonitor.from_config(config)
    scheduler = BackendBulkheadScheduler.from_config(config)
    prompt_builder = PromptBuilder()
    materialized_repo_service = MaterializedRepoService(
        repository,
        workspace_manager,
        config=config,
        markdown_field=orchestrator.markdown_field,
        active_task_id=orchestrator.active_task_id,
        active_workflow_type=orchestrator.active_workflow_type,
    )
    test_gate_service = TestGateService(
        config=config,
        repository=repository,
        artifact_manager=artifact_manager,
        latest_materialized_repo=materialized_repo_service.latest_materialized_repo,
        markdown_field=orchestrator.markdown_field,
    )
    patch_gate_service = PatchGateService(
        config=config,
        repository=repository,
        artifact_manager=artifact_manager,
        source_repo_for_task=materialized_repo_service.source_repo_for_existing_project_task,
        materialized_repo_dir=materialized_repo_service.materialized_repo_dir,
        copy_source=materialized_repo_service.copy_source_for_patch_validation,
        write_success_marker=materialized_repo_service.write_materialized_success_marker,
        emit=orchestrator.emit_progress,
        positive_int=orchestrator.positive_int,
    )
    input_staging_service = InputStagingService(
        config=config,
        repository=repository,
        visibility=artifact_visibility,
        judge=judge,
        repo_context_metadata=materialized_repo_service.repo_context_metadata,
        positive_int=orchestrator.positive_int,
    )
    agent_runner = AgentPhaseRunner(orchestrator)
    delivery_publisher = DeliveryPublisher(
        config=config,
        repository=repository,
        latest_usage_guide=communicator.latest_usage_guide,
        latest_materialized_repo=materialized_repo_service.latest_materialized_repo,
        source_repo_for_existing_project_task=materialized_repo_service.source_repo_for_existing_project_task,
    )
    workflow_engine = WorkflowEngine(orchestrator)
    return ApplicationServices(
        repository=repository,
        config_service=config_service,
        workspace_manager=workspace_manager,
        artifact_manager=artifact_manager,
        artifact_visibility=artifact_visibility,
        validator=validator,
        communicator=communicator,
        judge=judge,
        backend_health=backend_health,
        scheduler=scheduler,
        prompt_builder=prompt_builder,
        materialized_repo_service=materialized_repo_service,
        test_gate_service=test_gate_service,
        patch_gate_service=patch_gate_service,
        input_staging_service=input_staging_service,
        agent_runner=agent_runner,
        delivery_publisher=delivery_publisher,
        workflow_engine=workflow_engine,
        logger=get_logger(__name__),
    )

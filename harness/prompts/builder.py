from __future__ import annotations

from pathlib import Path
import re

from harness.agents.context import AgentRunContext
from harness.artifacts.delivery_codes import delivery_return_code_contract_lines, markdown_business_code_contract_lines


PLANNER_SPECIALIZATIONS: dict[int, list[tuple[str, str, list[str]]]] = {
    1: [
        (
            "Balanced Planner",
            "Balanced planning, covering MVP feasibility, architecture, risk control, testing strategy, and delivery quality.",
            [
                "Understand the user goal.",
                "Produce an executable plan.",
                "Control implementation complexity.",
                "Identify critical risks.",
                "Break work into Executor-ready todo items.",
                "Define clear validation criteria for Tester.",
            ],
        )
    ],
    2: [
        (
            "Pragmatic Planner",
            "MVP-first, implementation-oriented, complexity-minimizing.",
            [
                "Define the smallest viable end-to-end workflow.",
                "Identify what must be implemented in v1.",
                "Identify what can be mocked.",
                "Identify what can be deferred to v2.",
                "Reduce Executor implementation burden.",
            ],
        ),
        (
            "Robust Planner",
            "Reliability-first, failure-aware, recovery-oriented, maintainability-focused.",
            [
                "Check whether the state machine is well-defined.",
                "Identify potential deadlocks or infinite loops.",
                "Verify timeout, retry, and max_round coverage.",
                "Define handling for invalid Agent outputs.",
                "Ensure workspace isolation.",
                "Ensure artifact traceability and auditability.",
            ],
        ),
    ],
    3: [
        (
            "MVP Planner",
            "Minimal viable workflow, fast implementation, scope control.",
            [
                "Determine the fastest path to a working v1.",
                "Identify mandatory modules.",
                "Identify mockable modules.",
                "Identify deferrable modules.",
                "Minimize implementation steps.",
            ],
        ),
        (
            "Architecture Planner",
            "Modular architecture, clean boundaries, extensibility, long-term maintainability.",
            [
                "Define clear boundaries between Orchestrator, Adapter, Workspace, Artifact, and State Store.",
                "Keep data flow simple and explicit.",
                "Ensure the state machine is extensible.",
                "Ensure future integration with Claude Code or Codex is straightforward.",
                "Ensure future support for a socket broker or distributed workers.",
            ],
        ),
        (
            "Risk Planner",
            "Failure-mode analysis, concurrency safety, Agent containment, recovery design.",
            [
                "Handle Agent hangs.",
                "Handle invalid Agent outputs.",
                "Handle patch merge conflicts.",
                "Handle repeated test failures.",
                "Handle missing user information.",
                "Support recovery after interrupted execution.",
            ],
        ),
    ],
    4: [
        (
            "MVP Planner",
            "Minimal viable workflow, fast implementation, strict v1/v2 scope separation.",
            [
                "Build the fastest runnable system.",
                "Prioritize the main execution path.",
                "Reduce v1 complexity.",
                "Clearly separate v1 requirements from v2 extensions.",
            ],
        ),
        (
            "Architecture Planner",
            "System architecture, module boundaries, extensibility, maintainability.",
            [
                "Module decomposition.",
                "Data flow.",
                "State machine design.",
                "Extensibility.",
                "Maintainability.",
            ],
        ),
        (
            "Risk Planner",
            "Failure-mode coverage, concurrency safety, recovery mechanisms, operational resilience.",
            [
                "Deadlocks.",
                "Timeouts.",
                "Retries.",
                "Agent containment.",
                "Missing artifacts.",
                "Workspace contamination.",
                "Merge conflicts.",
            ],
        ),
        (
            "Delivery Planner",
            "User-goal alignment, final deliverability, acceptance criteria, explainability.",
            [
                "Define what the user ultimately receives.",
                "Define the contents of final_delivery.md.",
                "Make the result understandable.",
                "Define acceptance criteria.",
                "Document known limitations.",
                "Ensure the completed system is usable by the user.",
            ],
        ),
    ],
}

TESTER_SPECIALIZATIONS: dict[int, list[tuple[str, str, list[str]]]] = {
    1: [
        (
            "Balanced Tester",
            "Comprehensive validation across build, functionality, edge cases, integration, and regression.",
            [
                "Verify build or compile success.",
                "Verify startup behavior.",
                "Validate core functionality.",
                "Check obvious edge-case failures.",
                "Review whether changed files are reasonable.",
                "Ensure bug_report includes reproduction steps.",
            ],
        )
    ],
    2: [
        (
            "Build & Functional Tester",
            "Build verification, startup validation, core functional correctness.",
            [
                "Verify build or compile success.",
                "Verify application startup.",
                "Validate core functionality against selected_plan.",
                "Check primary happy paths.",
                "Run basic automated tests.",
            ],
        ),
        (
            "Edge & Integration Tester",
            "Edge-case coverage, integration correctness, regression risk detection.",
            [
                "Empty input and invalid input.",
                "Boundary inputs.",
                "Cross-module behavior.",
                "Whether merged patches break existing functionality.",
                "Regression risks.",
            ],
        ),
    ],
    3: [
        (
            "Build & Smoke Tester",
            "Build readiness, startup sanity, basic smoke validation.",
            [
                "Verify dependency installation.",
                "Verify compilation.",
                "Verify startup.",
                "Verify basic commands.",
                "Detect obvious runtime errors.",
            ],
        ),
        (
            "Functional Tester",
            "Requirement coverage, core behavior correctness, user-facing functionality.",
            [
                "Verify user requirements are satisfied.",
                "Verify selected_plan features are implemented.",
                "Validate core execution paths.",
                "Validate expected output format.",
                "Check primary use cases.",
            ],
        ),
        (
            "Edge & Regression Tester",
            "Edge-case robustness, exceptional-path behavior, regression protection.",
            [
                "Boundary inputs.",
                "Invalid inputs.",
                "Repeated execution.",
                "Failure recovery.",
                "Existing functionality preservation.",
                "New bugs introduced by multi-round fixes.",
            ],
        ),
    ],
    4: [
        (
            "Build & Smoke Tester",
            "Build readiness, startup sanity, blocking-error detection.",
            [
                "Verify build success.",
                "Verify compilation success.",
                "Verify startup success.",
                "Verify basic commands.",
                "Detect obvious crashes.",
                "Mark build failure as a blocking bug.",
            ],
        ),
        (
            "Functional & Acceptance Tester",
            "Requirement satisfaction, acceptance criteria validation, user-facing correctness.",
            [
                "Verify the original user request is satisfied.",
                "Verify selected_plan deliverables are completed.",
                "Validate core functional correctness.",
                "Validate output format.",
                "Verify the user can actually use the result.",
            ],
        ),
        (
            "Edge & Regression Tester",
            "Edge-case robustness, exceptional-path behavior, regression protection.",
            [
                "Empty input.",
                "Invalid input.",
                "Large input.",
                "Repeated execution.",
                "Partial failure.",
                "Whether fixes break existing functionality.",
            ],
        ),
        (
            "Integration & Risk Tester",
            "Integration correctness, state consistency, concurrency-risk detection, delivery-risk assessment.",
            [
                "Cross-module connectivity.",
                "Consistency after Executor artifact merging.",
                "Potential state-machine stalls.",
                "Artifact completeness.",
                "Workspace contamination.",
                "Concurrency conflicts.",
                "Delivery risk acceptability.",
            ],
        ),
    ],
}


class PromptBuilder:
    def build(self, context: AgentRunContext) -> str:
        input_artifacts = "\n".join(f"- {path}" for path in context.input_artifacts) or "- none"
        required_outputs = "\n".join(f"- {name}" for name in context.required_outputs) or "- none"
        role_specialization = self._role_specialization(context)
        metadata_lines = self._metadata_lines(context)
        return "\n".join(
            [
                "# Harness Agent Contract",
                "",
                f"Role: {context.role}",
                f"Phase: {context.phase}",
                f"Task ID: {context.task_id}",
                f"Phase ID: {context.phase_id}",
                f"Agent ID: {context.agent_id}",
                f"Round: {context.round_id}",
                "",
                "## User Request",
                context.user_prompt,
                "",
                "## Role Responsibility",
                context.role_instruction,
                "",
                "## Role Specialization",
                *role_specialization,
                "",
                "## Harness Metadata",
                *metadata_lines,
                "",
                "## Workspace Boundaries",
                f"- Workspace directory: {context.workspace_dir}",
                f"- Repository directory: {context.repo_dir}",
                f"- Input directory: {context.input_dir}",
                f"- Output directory: {context.output_dir}",
                f"- Log directory: {context.log_dir}",
                "",
                "## Input Artifacts",
                input_artifacts,
                "",
                "## Input Rules",
                f"- Input artifacts are local copies under {context.input_dir}.",
                f"- Read `{context.input_dir / 'manifest.md'}` before making decisions when it exists.",
                "- The manifest marks artifacts that Harness truncated or skipped to stay within the role input budget.",
                "- Treat truncated artifacts as partial evidence; call out missing evidence in your required reports instead of guessing.",
                "- Do not assume artifact source paths outside this workspace are readable.",
                "- Treat artifacts as authoritative evidence for previous phases.",
                "- This harness uses artifact-based patch merge. After PATCH_MERGE succeeds, the repository directory should contain Harness materialized source from `merged_patch.diff` when the patch can be applied.",
                "- `merged_patch.diff` is the authoritative implementation artifact after PATCH_MERGE exists.",
                "- `patch_validation.md`, when present, is Harness-generated evidence about whether `merged_patch.diff` can be applied with `git apply --check`.",
                "- `materialized_repo.md`, when present, records the Harness materialized repository path used for downstream role workspaces.",
                "- `objective_gate.md` and `test_gate.md`, when present, are Harness-generated hard-gate evidence that LLM roles cannot override.",
                "- Every `patch.diff` and `fix_patch.diff` patch artifact must have sibling `patch_metadata.md`; every `merged_patch.diff` artifact must have sibling `merged_patch_metadata.md`.",
                "- Patch metadata must declare `patch_artifact`, `base_source_type`, `base_source_path`, `base_round`, `base_task_id`, `apply_target`, `patch_scope`, `changed_files`, `expected_apply_command`, and `compatibility_notes`.",
                "- Valid `patch_scope` values are `full_project`, `incremental_fix`, and `merged_authoritative`.",
                "- Treat any patch artifact as invalid evidence when its metadata is missing, does not name that patch artifact, or declares a baseline/apply target incompatible with the current repository.",
                "- `patch.diff` and `fix_patch.diff` are candidate inputs for PATCH_MERGE only; tester, reviewer, judge, and communicator roles must not treat them as final deliverables.",
                "- Tester, reviewer, and judge roles must evaluate the repository directory, `merged_patch.diff`, `merge_report.md`, Harness gate reports, role reports, and summaries.",
                "",
                "## Required Output Files",
                required_outputs,
                "",
                "## Output Contract",
                f"- Write every required deliverable under this exact output directory: `{context.output_dir}`.",
                f"- Work on source files only under the repository directory: `{context.repo_dir}`.",
                "- Create every required output file before exiting.",
                "- Do not write final deliverables anywhere outside the output directory.",
                "- Do not overwrite input artifacts.",
                "- If you cannot complete the role contract, still write every possible required file and mark delivery.md with a non-zero return code.",
                "- Every role and every phase must create `delivery.md`.",
                *delivery_return_code_contract_lines(),
                *markdown_business_code_contract_lines(),
                "",
                "## Phase-Specific Rules",
                *self._phase_specific_rules(context),
                "",
                "## Prohibited Actions",
                "- Do not communicate directly with the user.",
                "- Do not switch phase.",
                "- Do not wait for, invoke, or coordinate with another agent.",
                "- Do not modify global task state, SQLite state, artifact registry, or orchestration metadata.",
                "- Do not create internal FileTool, ShellTool, EditTool, or TestTool systems.",
                "- Do not claim completion unless the required output files exist in the output directory.",
            ]
        )

    def _phase_specific_rules(self, context: AgentRunContext) -> list[str]:
        if context.role == "judge":
            if context.phase == "TEST_JUDGEMENT":
                return [
                    "- For TEST_JUDGEMENT, `decision.json` must contain a top-level `decision` string with value `pass` or `fail`; this JSON enum is state-machine data and is the only exception to the Markdown numeric-code rule.",
                    "- `decision.json` must include an `evidence` object summarizing objective gate facts, test command exit codes, changed files, and any blocking findings you relied on.",
                    "- `decision.json.decision` is the test verdict. It must not be copied into the `delivery.md` return code.",
                    "- If you choose `decision: fail` because tests failed, write `return_code: 0` in `delivery.md` as long as `decision.json`, `decision_summary.md`, and `delivery.md` are complete.",
                    "- `decision_summary.md` must include `artifact_result_code: 0` as its first non-empty line and one machine-readable line: `decision_code: 0` for pass or `decision_code: -1` for fail.",
                    "- Do not decide objective facts from natural-language reports. Use structured Harness evidence in `objective_gate.md`, `patch_validation.md`, `materialized_repo.md`, and `test_gate.md`.",
                    "- Use `pass` only when structured evidence shows patch apply check passed, diff check passed, build/test commands passed or were explicitly not required, and the merged patch is coherent and testable.",
                    "- Use `fail` when tests failed, required evidence is missing, `merged_patch.diff` is missing, `merged_patch_metadata.md` is missing or incompatible, any Harness gate reports `status: fail`, or the merged patch is not testable.",
                ]
            if context.phase in {"REVIEW_JUDGEMENT", "FINAL_JUDGEMENT", "PLAN_JUDGEMENT"}:
                rules = [
                    f"- For {context.phase}, `decision.json` must contain a top-level `decision` string with value `approved` or `changes_required`; this JSON enum is state-machine data and is the only exception to the Markdown numeric-code rule.",
                    "- Use `approved` only when the artifact set satisfies the current phase contract.",
                    "- Use `changes_required` when required artifacts are missing, evidence is weak, or unresolved risks block progression.",
                    "- `decision.json.decision` is the phase verdict. It must not be copied into the `delivery.md` return code.",
                    "- If you choose `changes_required`, write `return_code: 0` in `delivery.md` as long as `decision.json`, `decision_summary.md`, and `delivery.md` are complete.",
                    "- `decision_summary.md` must include `artifact_result_code: 0` as its first non-empty line and one machine-readable line: `decision_code: 0` for approved or `decision_code: 1` for changes_required.",
                    "- `decision.json` must include an `evidence` object with the structured facts used for the decision; keep semantic judgement separate from objective gate facts.",
                ]
                if context.phase in {"REVIEW_JUDGEMENT", "FINAL_JUDGEMENT"}:
                    rules.extend(
                        [
                            "- Treat `merged_patch.diff` as the authoritative implementation artifact.",
                            "- Treat `merged_patch_metadata.md` as required baseline evidence for the authoritative implementation artifact.",
                            "- Treat `patch_validation.md` as Harness evidence for whether the authoritative patch applies cleanly.",
                            "- Treat `objective_gate.md` and `test_gate.md` as hard Harness evidence; do not approve when either reports fail.",
                            "- Do not approve based on raw `patch.diff` or `fix_patch.diff` when `merged_patch.diff` is absent, metadata is absent, or metadata is inconsistent.",
                        ]
                    )
                return rules
        if context.role == "executor" and context.phase == "MISC_RESPONSE":
            return [
                "- This is an informational response workflow, not an implementation workflow.",
                "- Do not create or modify project files.",
                "- `response.md` must answer the user's request directly, using any staged historical artifacts as context when relevant.",
                "- `notes.md` must summarize what context was used, assumptions made, and any limitations.",
                "- If the user asks for an action that would modify files, state that the request should be routed to bugfix, feature_change, or new_project instead.",
            ]
        if context.role == "executor" and context.phase == "PATCH_MERGE":
            return [
                "- This is the model-driven PATCH_MERGE phase.",
                "- `merge_report.md` and `merged_patch_metadata.md` must each start with `artifact_result_code: 0` when complete.",
                "- Read all candidate `patch.diff`, `fix_patch.diff`, and `patch_metadata.md` artifacts listed in the input manifest.",
                "- Before selecting any candidate patch, verify its `patch_metadata.md` names the exact patch artifact and declares a baseline/apply target compatible with the current repository directory.",
                "- Do not select a patch based only on filename, artifact version, model wording, or previous role confidence.",
                "- Prior `merged_patch.diff` artifacts are historical evidence, not candidates to reuse, unless `merged_patch_metadata.md` proves they target the same baseline and apply target as the current repository.",
                "- Reject or omit candidates whose metadata is missing, names a different patch, has stale `base_round` or `base_task_id`, uses a `full_project` scope against an already materialized project, or otherwise conflicts with the current repository baseline.",
                "- Produce exactly one authoritative `merged_patch.diff` that represents the implementation candidate downstream roles must test, review, judge, and deliver.",
                "- Produce `merged_patch_metadata.md` next to `merged_patch.diff`; it must declare `patch_artifact: merged_patch.diff`, selected candidate metadata, baseline compatibility decision, current `apply_target`, and `patch_scope: merged_authoritative`.",
                "- Do not concatenate blindly. Resolve overlaps, choose compatible changes, and explain any omitted or adjusted candidate patch in `merge_report.md`.",
                "- `merged_patch.diff` must be a valid unified diff. If the candidate patches cannot be merged into a coherent diff, still write the best safe subset when possible and set the first line of `delivery.md` to a non-zero `return_code`.",
                "- Generate `merged_patch.diff` into the output directory via shell redirection or file operations; do not paste a large merged diff as a Write-tool payload.",
                "- Do not print full candidate patches or the full merged patch to stdout. Use `wc -c`, diff stats, and short excerpts only when verifying.",
                "- `merge_report.md` must state selected candidate artifacts, rejected candidate artifacts, metadata compatibility checks, conflict handling, known risks, and whether the merged patch is ready for testing.",
            ]
        if context.role == "executor":
            return [
                "- If the repository is empty, still produce implementation artifacts and a complete unified diff representing the proposed files.",
                "- All Markdown deliverables you produce, such as `implementation_plan.md`, `changed_files.md`, `patch_metadata.md`, `fix_schedule.md`, `fix_notes.md`, and `self_check.md`, must start with `artifact_result_code: 0` when complete.",
                "- If the repository already contains materialized source from a previous PATCH_MERGE, make fix changes against that repository state.",
                "- `patch.diff` or `fix_patch.diff` must be a valid unified diff that could create or update implementation files.",
                "- Produce `patch_metadata.md` next to the patch. It must name the exact patch file, the current repository baseline, the intended apply target, the patch scope, changed files, and the expected apply command.",
                "- For FIXING and REVIEW_FIXING, prefer `patch_scope: incremental_fix` and target the current materialized/source repository; do not describe a historical empty project baseline unless the current repository is actually empty.",
                "- Create or update implementation files under the repository directory first, then generate `patch.diff` or `fix_patch.diff` mechanically from repository changes.",
                f"- Prefer command-generated diffs written directly to the output directory. For git repositories, use `git add -N . && git diff --no-ext-diff -- . > {context.output_dir / 'patch.diff'}` or the corresponding `fix_patch.diff` path.",
                "- If the repository is not a git worktree, initialize a temporary git baseline or use a script/diff command that writes unified diff output directly to the required patch file.",
                "- Do not paste a large patch into a Write-tool payload, and do not `cat` or print the full patch to stdout. Verify large patches with `wc -c`, file counts, and diff stats.",
                "- Avoid duplicating full source code in markdown deliverables; summarize file-level changes and reference paths instead.",
                "- Create `delivery.md` and `self_check.md` early with the current return code, then update them before exit.",
                "- `changed_files.md` or `fix_notes.md` must list the intended file-level changes and rationale.",
                "- `self_check.md` must describe verification performed, unverified assumptions, and remaining risks.",
            ]
        if context.role == "tester":
            return [
                "- Use `merged_patch.diff` as the implementation under test whenever it exists.",
                "- `build_report.md`, `test_report.md`, and `bug_report.md` must each start with `artifact_result_code: 0` when complete.",
                "- Prefer running build, tests, and smoke checks directly in the repository directory when it contains materialized source.",
                "- Read `materialized_repo.md` when present to understand which Harness materialized source snapshot was copied into the repository directory.",
                "- Read `patch_validation.md` when present. If it reports `status: fail`, report testing as fail unless you have stronger direct evidence from applying and testing the patch yourself.",
                "- Read `objective_gate.md` and `test_gate.md` when present; if either reports `status: fail`, report testing as fail.",
                "- Read `merged_patch_metadata.md` when present; fail testing if the authoritative patch metadata is missing or incompatible with the repository under test.",
                "- Treat raw `patch.diff` and `fix_patch.diff` as non-authoritative candidate inputs; do not pass a task based only on raw candidate patches.",
                "- If no merged repository exists, report that the implementation is not ready for testing unless the current phase explicitly predates PATCH_MERGE.",
                "- `build_report.md` must describe setup/build outcome or explain why build execution was not possible, and include `build_result_code: 0` for build passed/not required, `build_result_code: -1` for build failed, or `build_result_code: 2` for blocked/not run.",
                "- `test_report.md` must include one machine-readable line: `test_result_code: 0` for tests passed, `test_result_code: -1` for tests failed, or `test_result_code: 2` for blocked/not testable.",
                "- `test_result_code` is the test verdict. It must not be copied into the `delivery.md` return code.",
                "- `bug_report.md` must list blocking bugs, non-blocking issues, and reproduction details when available, and include `bug_result_code: 0` for no blocking bugs, `bug_result_code: 1` for non-blocking issues only, or `bug_result_code: -1` for blocking bugs.",
            ]
        if context.role == "planner":
            if context.phase == "PLANNING_PEER_REVIEW":
                return [
                    "- This is a planner peer-review phase coordinated by Harness artifacts.",
                    "- `peer_review.md` must start with `artifact_result_code: 0` when complete.",
                    "- Read the input manifest and review plans from every other planner agent; do not review your own plan as if it were external feedback.",
                    "- Write `peer_review.md` with your `agent_id`, the reviewed planner agent IDs, concrete approval or objection notes, and any blocking issues.",
                    "- `peer_review.md` must include one machine-readable line: `peer_review_code: 0` when all reviewed plans are acceptable, `peer_review_code: 1` when any plan needs revision, or `peer_review_code: -1` for a blocking objection.",
                    "- `peer_review_code` is the review verdict. It must not be copied into the `delivery.md` return code.",
                    "- If you disagree with another planner, write the reason and the exact artifact section or file-level planning choice you object to.",
                ]
            if context.phase == "PLANNING_REVISION":
                return [
                    "- This is a planner revision phase after peer review.",
                    "- `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md` must each start with `artifact_result_code: 0` when complete.",
                    "- Read all available `peer_review.md` artifacts, especially comments directed at your previous plan.",
                    "- Revise `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md` based on feedback you accept.",
                    "- When you reject feedback, state the rejected feedback and rationale in `plan.md` or `risk.md` instead of silently ignoring it.",
                    *self._todo_breakdown_schema_rules(),
                ]
            return [
                "- `plan.md` must describe the proposed approach, architecture or code areas affected, and acceptance criteria.",
                "- `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md` must each start with `artifact_result_code: 0` when complete.",
                "- `assumptions.md` must separate verified facts from assumptions.",
                "- `risk.md` must identify technical, integration, testing, and delivery risks.",
                *self._todo_breakdown_schema_rules(),
            ]
        if context.role == "reviewer":
            if context.phase == "PLAN_REVIEW":
                return [
                    "- This is a planning review phase after planner peer-review loops.",
                    "- `review_report.md` and `selected_plan.md` must each start with `artifact_result_code: 0` when complete.",
                    "- Review all planner `plan.md`, `assumptions.md`, `risk.md`, `todo_breakdown.md`, and `peer_review.md` artifacts against the user's request.",
                    "- `review_report.md` must select the best planner proposal by `agent_id` and explain why it should guide executor agents.",
                    "- If no single proposal is sufficient, choose the best base proposal and list required adjustments.",
                    "- `selected_plan.md` must consolidate the chosen proposal into the single authoritative plan for executor agents.",
                    "- `selected_plan.md` must include files, steps, acceptance criteria, test commands, dependencies, and risks using the planner todo schema.",
                    "- `review_report.md` must include one machine-readable line: `review_decision_code: 0` when the selected proposal is actionable enough for execution, `review_decision_code: 1` when changes are required, or `review_decision_code: -1` for blocking rejection.",
                    "- `review_decision_code` is the review verdict. It must not be copied into the `delivery.md` return code.",
                ]
            return [
                "- `review_report.md` must start with `artifact_result_code: 0` when complete.",
                "- Review `merged_patch.diff` as the authoritative implementation artifact whenever it exists.",
                "- Review `merged_patch_metadata.md` as required baseline/apply-target evidence for the authoritative patch.",
                "- Review `patch_validation.md` as Harness-generated apply-check evidence whenever it exists.",
                "- Treat raw `patch.diff` and `fix_patch.diff` as background candidates only, not as the delivered implementation.",
                "- `review_report.md` must include one machine-readable line: `review_decision_code: 0` for approved, `review_decision_code: 1` for changes required, or `review_decision_code: -1` for blocking rejection.",
                "- `review_decision_code` is the review verdict. It must not be copied into the `delivery.md` return code.",
                "- Review correctness, scope control, regressions, test adequacy, security, and maintainability.",
                "- When changes are required, include concrete fix instructions and affected artifacts.",
            ]
        if context.role == "communicator":
            return [
                "- `final_delivery.md` and `usage_guide.md` must each start with `artifact_result_code: 0` when complete.",
                "- `final_delivery.md` must summarize final outcome code, completed work, artifact paths or names, validation result, and known risks.",
                "- `final_delivery.md` must include one machine-readable line: `final_delivery_code: 0` for accepted final delivery, `final_delivery_code: 1` for partial delivery, `final_delivery_code: 2` for blocked delivery, or `final_delivery_code: -1` for failed delivery.",
                "- `final_delivery.md` must include a short handoff section with exactly these fields: `project_dir`, `run_command`, and `dependency_install`.",
                "- `project_dir` must point to the delivered source/project directory when available, not merely to Harness internal artifact files.",
                "- `run_command` must be the exact command the user should execute from the project directory.",
                "- Before deciding dependency instructions, inspect the delivered project files and referenced run/test commands for third-party runtime or test dependencies that may be missing from the user's current Python/Node/system environment.",
                "- `dependency_install` must be an exact one-command dependency installer when dependencies exist; prefer `bash install_dependencies.sh` or a command that creates an isolated environment and installs from `requirements.txt`/lock files. Use `none` only when no dependency installation is required.",
                "- The expected success path is precomputed before publishing; Harness will create/copy the final files there after this communicator phase succeeds.",
                "- `usage_guide.md` must explain how to use the delivered result.",
                "- `usage_guide.md` must include an `## Actual Usage` section written for the end user, not for Harness internals.",
                "- In `## Actual Usage`, provide the exact sequence the user should run: enter project directory, install dependencies when needed, run the program or tests, pass required inputs/configuration, and verify success.",
                "- The actual usage instructions must be grounded in the delivered files and commands. If a command cannot be verified from artifacts, say what must be confirmed instead of pretending it is executable.",
                "- `usage_guide.md` must include prerequisites, the one-command dependency installer, setup steps, run commands, configuration values, verification steps, and common failure modes.",
                "- Put paths and commands in fenced or inline code blocks so they remain copyable and are not translated in the UI.",
                "- Keep `usage_guide.md` practical and task-specific. Do not repeat generic Harness internals unless they are needed to use the delivery.",
                "- Do not invent implementation details that are not supported by artifacts.",
            ]
        return []

    def _todo_breakdown_schema_rules(self) -> list[str]:
        return [
            "- `todo_breakdown.md` must provide executable work items suitable for an executor role.",
            "- `todo_breakdown.md` must use this exact repeated task schema so executor agents receive consistent plans:",
            "  - `## Task <number>: <short imperative title>`",
            "  - `files: <target paths or path globs>`",
            "  - `steps:` with ordered implementation steps",
            "  - `acceptance_criteria:` with concrete observable outcomes",
            "  - `test_commands:` with exact commands or `not_applicable: <reason>`",
            "  - `dependencies:` with prerequisite task numbers or `none`",
            "  - `risk_notes:` with task-specific risks or `none`",
            "- Keep each task scoped so an executor can implement it without inferring missing files, commands, or acceptance criteria.",
        ]

    def _metadata_lines(self, context: AgentRunContext) -> list[str]:
        if not context.metadata:
            return ["- none"]
        lines: list[str] = []
        for key in sorted(context.metadata):
            value = context.metadata[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                lines.append(f"- {key}: {value}")
            else:
                lines.append(f"- {key}: {value!r}")
        return lines

    def _role_specialization(self, context: AgentRunContext) -> list[str]:
        specializations_by_role = {
            "planner": PLANNER_SPECIALIZATIONS,
            "tester": TESTER_SPECIALIZATIONS,
        }.get(context.role)
        if not specializations_by_role:
            return ["- No additional role specialization for this agent."]

        configured_count = self._configured_role_count(context)
        profile_count = min(max(configured_count, 1), 4)
        profiles = specializations_by_role[profile_count]
        agent_index = self._agent_index(context.agent_id)
        if agent_index < 1 or agent_index > len(profiles):
            return [
                "- Specialization: Balanced overflow agent.",
                "- Preference: Support the role contract without duplicating another agent's exact emphasis.",
                "- Focus:",
                "  - Cover gaps left by available input artifacts.",
                "  - Keep outputs concrete, evidence-based, and usable by downstream roles.",
            ]

        name, preference, focus_items = profiles[agent_index - 1]
        return [
            f"- Specialization: {name}.",
            f"- Preference: {preference}",
            "- Focus:",
            *(f"  - {item}" for item in focus_items),
        ]

    def _configured_role_count(self, context: AgentRunContext) -> int:
        try:
            return int(context.config.get("roles", {}).get(context.role, {}).get("count", 1))
        except (TypeError, ValueError):
            return 1

    def _agent_index(self, agent_id: str) -> int:
        match = re.search(r"-(\d+)$", agent_id)
        return int(match.group(1)) if match else 1


def render_artifact_list(paths: list[Path]) -> str:
    return "\n".join(str(path) for path in paths)

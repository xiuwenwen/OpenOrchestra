from __future__ import annotations

from pathlib import Path
import re

from harness.agents.context import AgentRunContext


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
                "- This harness uses artifact-based patch merge in MVP mode. The repository directory may be empty in later phases.",
                "- `merged_patch.diff` is the authoritative implementation artifact after PATCH_MERGE exists.",
                "- `patch.diff` and `fix_patch.diff` are candidate inputs for PATCH_MERGE only; tester, reviewer, judge, and communicator roles must not treat them as final deliverables.",
                "- Tester, reviewer, and judge roles must evaluate `merged_patch.diff`, `merge_report.md`, reports, and summaries, not fail solely because the repo directory is empty.",
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
                "- If you cannot complete the role contract, still write every possible required file and mark delivery.md accordingly.",
                "- Every role and every phase must create `delivery.md`.",
                "- `delivery.md` must contain exactly one status line: `status: success`, `status: failed`, or `status: partial`.",
                "- Markdown display styling around the status field is allowed, but the field content must normalize exactly to `status: success`, `status: failed`, or `status: partial`.",
                "- The status value must contain only `success`, `failed`, or `partial`; do not append explanations to the value.",
                "- Use `status: success` only when all required outputs are complete and this role's contract is satisfied.",
                "- Use `status: failed` when the role could not produce a usable result.",
                "- Use `status: partial` when some useful output exists but the role contract is incomplete.",
                "- `delivery.md` must state task status, role success status, produced files, and known risks.",
                "- Harness validates `delivery.md`; any status other than `success` prevents the run from advancing.",
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
                    "- For TEST_JUDGEMENT, `decision.json` must contain a top-level `decision` string with value `pass` or `fail`.",
                    "- Use `pass` only when tester artifacts indicate success and `merged_patch.diff` is present, coherent, and testable.",
                    "- Use `fail` when tests failed, required evidence is missing, `merged_patch.diff` is missing, or the merged patch is not testable.",
                ]
            if context.phase in {"REVIEW_JUDGEMENT", "FINAL_JUDGEMENT", "PLAN_JUDGEMENT"}:
                rules = [
                    f"- For {context.phase}, `decision.json` must contain a top-level `decision` string with value `approved` or `changes_required`.",
                    "- Use `approved` only when the artifact set satisfies the current phase contract.",
                    "- Use `changes_required` when required artifacts are missing, evidence is weak, or unresolved risks block progression.",
                ]
                if context.phase in {"REVIEW_JUDGEMENT", "FINAL_JUDGEMENT"}:
                    rules.extend(
                        [
                            "- Treat `merged_patch.diff` as the authoritative implementation artifact.",
                            "- Do not approve based on raw `patch.diff` or `fix_patch.diff` when `merged_patch.diff` is absent or inconsistent.",
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
                "- Read all candidate `patch.diff` and `fix_patch.diff` artifacts listed in the input manifest.",
                "- Produce exactly one authoritative `merged_patch.diff` that represents the implementation candidate downstream roles must test, review, judge, and deliver.",
                "- Do not concatenate blindly. Resolve overlaps, choose compatible changes, and explain any omitted or adjusted candidate patch in `merge_report.md`.",
                "- `merged_patch.diff` must be a valid unified diff. If the candidate patches cannot be merged into a coherent diff, still write `merged_patch.diff` with the best safe subset and mark `delivery.md` as `failed` or `partial`.",
                "- Generate `merged_patch.diff` into the output directory via shell redirection or file operations; do not paste a large merged diff as a Write-tool payload.",
                "- Do not print full candidate patches or the full merged patch to stdout. Use `wc -c`, diff stats, and short excerpts only when verifying.",
                "- `merge_report.md` must state selected candidate artifacts, rejected candidate artifacts, conflict handling, known risks, and whether the merged patch is ready for testing.",
            ]
        if context.role == "executor":
            return [
                "- If the repository is empty, still produce implementation artifacts and a complete unified diff representing the proposed files.",
                "- `patch.diff` or `fix_patch.diff` must be a valid unified diff that could create or update implementation files.",
                "- Create or update implementation files under the repository directory first, then generate `patch.diff` or `fix_patch.diff` mechanically from repository changes.",
                f"- Prefer command-generated diffs written directly to the output directory. For git repositories, use `git add -N . && git diff --no-ext-diff -- . > {context.output_dir / 'patch.diff'}` or the corresponding `fix_patch.diff` path.",
                "- If the repository is not a git worktree, initialize a temporary git baseline or use a script/diff command that writes unified diff output directly to the required patch file.",
                "- Do not paste a large patch into a Write-tool payload, and do not `cat` or print the full patch to stdout. Verify large patches with `wc -c`, file counts, and diff stats.",
                "- Avoid duplicating full source code in markdown deliverables; summarize file-level changes and reference paths instead.",
                "- Create `delivery.md` and `self_check.md` early with current status, then update them before exit.",
                "- `changed_files.md` or `fix_notes.md` must list the intended file-level changes and rationale.",
                "- `self_check.md` must describe verification performed, unverified assumptions, and remaining risks.",
            ]
        if context.role == "tester":
            return [
                "- Use `merged_patch.diff` as the implementation under test whenever it exists.",
                "- Treat raw `patch.diff` and `fix_patch.diff` as non-authoritative candidate inputs; do not pass a task based only on raw candidate patches.",
                "- If no merged repository exists, report that the implementation is not ready for testing unless the current phase explicitly predates PATCH_MERGE.",
                "- `build_report.md` must describe setup/build outcome or explain why build execution was not possible.",
                "- `test_report.md` must clearly state `pass` or `fail` with evidence.",
                "- `bug_report.md` must list blocking bugs, non-blocking issues, and reproduction details when available.",
            ]
        if context.role == "planner":
            if context.phase == "PLANNING_PEER_REVIEW":
                return [
                    "- This is a planner peer-review phase coordinated by Harness artifacts.",
                    "- Read the input manifest and review plans from every other planner agent; do not review your own plan as if it were external feedback.",
                    "- Write `peer_review.md` with your `agent_id`, the reviewed planner agent IDs, concrete approval or objection notes, and any blocking issues.",
                    "- `peer_review.md` must include one machine-readable line: `status: satisfied` when all reviewed plans are acceptable, or `status: changes_requested` when any plan needs revision.",
                    "- If you disagree with another planner, write the reason and the exact artifact section or file-level planning choice you object to.",
                ]
            if context.phase == "PLANNING_REVISION":
                return [
                    "- This is a planner revision phase after peer review.",
                    "- Read all available `peer_review.md` artifacts, especially comments directed at your previous plan.",
                    "- Revise `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md` based on feedback you accept.",
                    "- When you reject feedback, state the rejected feedback and rationale in `plan.md` or `risk.md` instead of silently ignoring it.",
                    "- `todo_breakdown.md` must remain executable by executor agents and include a concise implementation sequence.",
                ]
            return [
                "- `plan.md` must describe the proposed approach, architecture or code areas affected, and acceptance criteria.",
                "- `assumptions.md` must separate verified facts from assumptions.",
                "- `risk.md` must identify technical, integration, testing, and delivery risks.",
                "- `todo_breakdown.md` must provide executable work items suitable for an executor role.",
            ]
        if context.role == "reviewer":
            if context.phase == "PLAN_REVIEW":
                return [
                    "- This is a planning review phase after planner peer-review loops.",
                    "- Review all planner `plan.md`, `assumptions.md`, `risk.md`, `todo_breakdown.md`, and `peer_review.md` artifacts against the user's request.",
                    "- `review_report.md` must select the best planner proposal by `agent_id` and explain why it should guide executor agents.",
                    "- If no single proposal is sufficient, choose the best base proposal and list required adjustments.",
                    "- State `approved` when the selected proposal is actionable enough for execution; otherwise state `changes_required`.",
                ]
            return [
                "- Review `merged_patch.diff` as the authoritative implementation artifact whenever it exists.",
                "- Treat raw `patch.diff` and `fix_patch.diff` as background candidates only, not as the delivered implementation.",
                "- `review_report.md` must state `approved` or `changes_required`.",
                "- Review correctness, scope control, regressions, test adequacy, security, and maintainability.",
                "- When changes are required, include concrete fix instructions and affected artifacts.",
            ]
        if context.role == "communicator":
            return [
                "- `final_delivery.md` must summarize final status, completed work, artifact paths or names, validation result, and known risks.",
                "- `final_delivery.md` must include the success path, source/project directory path when available, and the exact commands a user should run next.",
                "- `usage_guide.md` must explain how to use the delivered result.",
                "- `usage_guide.md` must include prerequisites, setup steps, run commands, configuration values, verification steps, common failure modes, and where relevant artifacts are stored.",
                "- Put paths and commands in fenced or inline code blocks so they remain copyable and are not translated in the UI.",
                "- Keep `usage_guide.md` practical and task-specific. Do not repeat generic Harness internals unless they are needed to use the delivery.",
                "- Do not invent implementation details that are not supported by artifacts.",
            ]
        return []

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

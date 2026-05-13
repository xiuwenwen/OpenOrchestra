from __future__ import annotations

from pathlib import Path
import re

from harness.agents.context import AgentRunContext
from harness.contracts.role_contracts import output_contract_lines_for
from harness.core.workflow_type import BUGFIX, FEATURE_CHANGE
from harness.prompts.specializations import (
    BUGFIX_PLANNER_SPECIALIZATIONS,
    FIX_MODIFY_PLANNER_SPECIALIZATIONS,
    PLANNER_SPECIALIZATIONS,
    TESTER_SPECIALIZATIONS,
)


class PromptBuilder:
    def build(self, context: AgentRunContext) -> str:
        input_artifacts = "\n".join(f"- {path}" for path in context.input_artifacts) or "- none"
        required_outputs = "\n".join(f"- {name}" for name in context.required_outputs) or "- none"
        required_output_paths = "\n".join(
            f"- `{name}`: `{context.output_dir / name}`" for name in context.required_outputs
        ) or "- none"
        role_specialization = self._role_specialization(context)
        metadata_lines = self._metadata_lines(context)
        sections = [
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
        ]
        if role_specialization:
            sections.extend(["## Role Specialization", *role_specialization, ""])
        sections.extend(
            [
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
                "- Harness gate reports, when present, are hard-gate evidence that LLM roles cannot override.",
                "- Every `merged_patch.diff` artifact must have sibling `merged_patch_metadata.md`.",
                "- Merged patch metadata must declare `patch_artifact`, `base_source_type`, `base_source_path`, `base_round`, `base_task_id`, `apply_target`, `patch_scope`, `changed_files`, `expected_apply_command`, and `compatibility_notes`.",
                "- Valid merged `patch_scope` value is `merged_authoritative`.",
                "- `patch.diff` and `fix_patch.diff` are candidate inputs for PATCH_MERGE only; tester, reviewer, judge, and communicator roles must not treat them as final deliverables.",
                "- Tester roles must evaluate the runnable repository directory directly; do not depend on executor narrative reports.",
                "- Reviewer and judge roles must use only the repository directory and artifacts explicitly listed in this prompt; do not search for hidden historical artifacts.",
                "",
                "## Required Output Files",
                required_outputs,
                "",
                "## Required Output Paths",
                "Write these exact files. Copy paths exactly.",
                required_output_paths,
                "- Before exiting, verify every exact path above exists.",
                "- A similarly named file under any other path is invalid.",
                "",
                "## Output Templates",
                "- Harness pre-creates editable templates for non-diff required output files in the output directory.",
                "- Keep exact machine-readable contract fields already present in the templates, such as `artifact_result_code: 0`.",
                "- Replace template body text with the completed deliverable content.",
                "- Remove every `harness_template_status: pending_model_completion` line and every JSON `harness_template_status` key before exiting.",
                "- Required `.diff` outputs are not pre-created templates; generate them from actual repository changes.",
                "",
                "## Output Contract",
                f"- Write every required deliverable under this exact output directory: `{context.output_dir}`.",
                f"- Work on source files only under the repository directory: `{context.repo_dir}`.",
                "- Create every required output file before exiting.",
                "- Do not write final deliverables anywhere outside the output directory.",
                "- Do not overwrite input artifacts.",
                *self._output_contract_lines(context),
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
        return "\n".join(sections)

    def _output_contract_lines(self, context: AgentRunContext) -> list[str]:
        return output_contract_lines_for(context.role, context.phase, context.required_outputs)

    def _phase_specific_rules(self, context: AgentRunContext) -> list[str]:
        if context.role == "judge":
            if context.phase == "TEST_JUDGEMENT":
                return [
                    "- For TEST_JUDGEMENT, `decision.json` must contain a top-level `decision` string with value `pass` or `fail`; this JSON enum is state-machine data and is the only exception to the Markdown numeric-code rule.",
                    "- `decision.json` must include an `evidence` object summarizing objective gate facts, test command exit codes, changed files, and any blocking findings you relied on.",
                    "- `decision.json.decision` is the test verdict. It must not be copied into the `delivery.md` return code.",
                    "- If you choose `decision: fail` because tests failed, write JSON `return_code: 0` in `delivery.md` as long as `decision.json`, `decision_summary.md`, and `delivery.md` are complete.",
                    "- `decision_summary.md` must include `artifact_result_code: 0` and one machine-readable line: `decision_code: 0` for pass or `decision_code: -1` for fail.",
                    "- Do not decide objective facts from natural-language reports. Use structured Harness evidence in `objective_gate.md` and `test_gate.md`.",
                    "- Use `pass` only when structured gate evidence shows the objective patch gate passed and build/test commands passed or were explicitly not required.",
                    "- Use `fail` when tests failed, required gate evidence is missing, any Harness gate reports `status: fail`, or the tester report shows blocking bugs.",
                ]
            if context.phase in {"REVIEW_JUDGEMENT", "FINAL_JUDGEMENT", "PLAN_JUDGEMENT"}:
                rules = [
                    f"- For {context.phase}, `decision.json` must contain a top-level `decision` string with value `approved` or `changes_required`; this JSON enum is state-machine data and is the only exception to the Markdown numeric-code rule.",
                    "- Use `approved` only when the artifact set satisfies the current phase contract.",
                    "- Use `changes_required` when required artifacts are missing, evidence is weak, or unresolved risks block progression.",
                    "- `decision.json.decision` is the phase verdict. It must not be copied into the `delivery.md` return code.",
                    "- If you choose `changes_required`, write JSON `return_code: 0` in `delivery.md` as long as `decision.json`, `decision_summary.md`, and `delivery.md` are complete.",
                    "- `decision_summary.md` must include `artifact_result_code: 0` and one machine-readable line: `decision_code: 0` for approved or `decision_code: 1` for changes_required.",
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
                "- `merge_report.md` and `merged_patch_metadata.md` must each contain `artifact_result_code: 0` when complete.",
                "- Read all candidate `patch.diff` and `fix_patch.diff` artifacts listed in the input manifest.",
                "- Before selecting any candidate patch, verify it can be applied or safely translated against the current repository directory.",
                "- Do not select a patch based only on filename, artifact version, model wording, or previous role confidence.",
                "- Prior `merged_patch.diff` artifacts are historical evidence, not candidates to reuse, unless `merged_patch_metadata.md` proves they target the same baseline and apply target as the current repository.",
                "- Reject or omit candidates that conflict with the current repository baseline or cannot be reconciled into one coherent merged patch.",
                "- Produce exactly one authoritative `merged_patch.diff` that represents the implementation candidate downstream roles must test, review, judge, and deliver.",
                "- Produce `merged_patch_metadata.md` next to `merged_patch.diff`; it must declare `patch_artifact: merged_patch.diff`, selected candidate patch artifacts, baseline compatibility decision, current `apply_target`, and `patch_scope: merged_authoritative`.",
                "- Do not concatenate blindly. Resolve overlaps, choose compatible changes, and explain any omitted or adjusted candidate patch in `merge_report.md`.",
                "- `merged_patch.diff` must be a valid git-style unified diff with `diff --git` file headers and must apply with `git apply --check --whitespace=nowarn`.",
                "- If the candidate patches cannot be merged into a coherent git-style diff, still write the best safe subset when possible and describe unresolved merge risk in `merge_report.md`.",
                "- Generate `merged_patch.diff` into the output directory via shell redirection or file operations; do not paste a large merged diff as a Write-tool payload.",
                "- Do not print full candidate patches or the full merged patch to stdout. Use `wc -c`, diff stats, and short excerpts only when verifying.",
                "- `merge_report.md` must state selected candidate artifacts, rejected candidate artifacts, compatibility checks, conflict handling, known risks, and whether the merged patch is ready for testing.",
            ]
        if context.role == "executor":
            return [
                "- If the repository is empty, still produce implementation artifacts and a complete unified diff representing the proposed files.",
                "- All Markdown deliverables you produce, such as `implementation_plan.md`, `changed_files.md`, `fix_schedule.md`, `fix_notes.md`, and `self_check.md`, must contain `artifact_result_code: 0` when complete.",
                "- If the repository already contains materialized source from a previous PATCH_MERGE, make fix changes against that repository state.",
                "- `patch.diff` or `fix_patch.diff` must be a valid git-style unified diff with `diff --git` file headers and must apply with `git apply --check --whitespace=nowarn`.",
                "- For FIXING and REVIEW_FIXING, target the current materialized/source repository; do not describe a historical empty project baseline unless the current repository is actually empty.",
                "- Create or update implementation files under the repository directory first, then generate `patch.diff` or `fix_patch.diff` mechanically from repository changes.",
                f"- Prefer command-generated diffs written directly to the output directory. For git repositories, use `git add -N . && git diff --no-ext-diff -- . > {context.output_dir / 'patch.diff'}` or the corresponding `fix_patch.diff` path.",
                "- If the repository is not a git worktree, initialize a temporary git baseline or use a script/diff command that writes unified diff output directly to the required patch file.",
                "- Do not paste a large patch into a Write-tool payload, and do not `cat` or print the full patch to stdout. Verify large patches with `wc -c`, file counts, and diff stats.",
                "- Avoid duplicating full source code in markdown deliverables; summarize file-level changes and reference paths instead.",
                "- Create `delivery.md` as the required JSON role return envelope and create `self_check.md` early, then update both before exit.",
                "- `changed_files.md` or `fix_notes.md` must list the intended file-level changes and rationale.",
                "- `self_check.md` must describe verification performed, unverified assumptions, and remaining risks.",
            ]
        if context.role == "tester":
            return [
                "## Test Target",
                f"- Test this exact repository directory: `{context.repo_dir}`.",
                "- Treat the repository directory as the runnable implementation produced by Harness.",
                "- Compare observable behavior against the original user request in `## User Request` and the `## Harness Test Target` section of the input manifest.",
                "- Read the input manifest for repository source metadata, but do not require executor planning notes or patch narrative artifacts to decide the test verdict.",
                "",
                "## Required Test Work",
                "- Inspect project structure and identify the likely build, install, startup, and test commands from files in the repository.",
                "- Before declaring execution blocked by missing dependencies or setup problems, run safe project-declared setup/install commands in the configured test runtime or an isolated local environment.",
                "- If a test command fails because of missing dependencies, attempt the repository's documented dependency installation once, then rerun the relevant build/test command and record both attempts.",
                "- If the repository exposes runnable build, install, test, startup, CLI, or smoke commands and the environment can execute them safely, you must run them; do not replace runnable verification with static inspection.",
                "- Run the safest available build/import/static checks and existing automated tests; static inspection is only acceptable after documenting the exact command attempted and the concrete blocker.",
                "- Run at least one smoke or CLI-level check when the repository exposes an entry point and doing so is safe.",
                "- If the input manifest contains `## Harness Test Gate Evidence`, treat those command results as already executed by Harness for this exact round; inspect the referenced logs and avoid rerunning identical commands unless the evidence is incomplete, stale, or needs confirmation.",
                "- When Harness Test Gate already covered the standard build/test command successfully, focus your live execution on uncovered smoke checks, user-facing behavior, or suspicious gaps instead of repeating the same full command.",
                "- Verify the core user-facing requirements from the original request by execution whenever possible; if execution is blocked, include the blocker, attempted commands, and the smallest code-level evidence you used instead.",
                "- If build or test execution is blocked, do not declare the fix correct; report the blocked status and the exact missing evidence.",
                "- Record exact commands run, exit codes, and important output excerpts in `bug_report.md`.",
                "",
                "## Tester Output",
                "- Treat the repository directory as the implementation under test.",
                "- `bug_report.md` must contain `artifact_result_code: 0` when complete.",
                "- `bug_report.md` is the single tester report. Include build, test, bug, evidence, and reproduction sections in this one file.",
                "- Prefer running build, tests, and smoke checks directly in the repository directory when it contains materialized source.",
                "- Use staged Harness evidence only when it is explicitly present in the input manifest; otherwise validate by executing or inspecting the repository directory.",
                "- Do not treat executor planning notes, self-checks, or change summaries as test evidence.",
                "- If you can run a relevant test or smoke check but did not run it, set `test_result_code: -1` and `bug_result_code: -1` because the validation contract was not satisfied.",
                "- If no merged repository exists, report that the implementation is not ready for testing unless the current phase explicitly predates PATCH_MERGE.",
                "- In `bug_report.md`, describe setup/build outcome or explain why build execution was not possible, and include `build_result_code: 0` for build passed/not required, `build_result_code: -1` for build failed, or `build_result_code: 2` for blocked/not run.",
                "- In `bug_report.md`, include one machine-readable line: `test_result_code: 0` for tests passed, `test_result_code: -1` for tests failed, or `test_result_code: 2` for blocked/not testable.",
                "- `test_result_code` is the test verdict. It must not be copied into the `delivery.md` return code.",
                "- `bug_report.md` must list blocking bugs, non-blocking issues, and reproduction details when available, and include `bug_result_code: 0` for no blocking bugs, `bug_result_code: 1` for non-blocking issues only, or `bug_result_code: -1` for blocking bugs.",
            ]
        if context.role == "planner":
            if context.phase == "PLANNING_PEER_REVIEW":
                return [
                    "- This is a planner peer-review phase coordinated by Harness artifacts.",
                    "- `peer_review.md` must contain `artifact_result_code: 0` when complete.",
                    "- Read the input manifest and review plans from every other planner agent; do not review your own plan as if it were external feedback.",
                    "- Write `peer_review.md` with your `agent_id`, the reviewed planner agent IDs, concrete approval or objection notes, and any blocking issues.",
                    "- `peer_review.md` must include one machine-readable line: `peer_review_code: 0` when all reviewed plans are acceptable, `peer_review_code: 1` when any plan needs revision, or `peer_review_code: -1` for a blocking objection.",
                    "- `peer_review_code` is the review verdict. It must not be copied into the `delivery.md` return code.",
                    "- If you disagree with another planner, write the reason and the exact artifact section or file-level planning choice you object to.",
                ]
            if context.phase == "PLANNING_REVISION":
                return [
                    "- This is a planner revision phase after peer review or planning merge-review feedback.",
                    "- `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md` must each contain `artifact_result_code: 0` when complete.",
                    "- If a reviewer `review_report.md` from PLAN_REVIEW is present with `review_decision_code: 1` or `review_decision_code: -1`, treat it as the authoritative revision request and revise only against that feedback.",
                    "- When revising from PLAN_REVIEW feedback, do not re-litigate old planner proposals or peer reviews unless the reviewer explicitly references them.",
                    "- Read all available `peer_review.md` artifacts, especially comments directed at your previous plan.",
                    "- Revise `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md` based on feedback you accept.",
                    "- When you reject feedback, state the rejected feedback and rationale in `plan.md` or `risk.md` instead of silently ignoring it.",
                    *self._todo_breakdown_schema_rules(),
                ]
            return [
                "- `plan.md` must describe the proposed approach, architecture or code areas affected, and acceptance criteria.",
                "- `plan.md`, `assumptions.md`, `risk.md`, and `todo_breakdown.md` must each contain `artifact_result_code: 0` when complete.",
                "- `assumptions.md` must separate verified facts from assumptions.",
                "- `risk.md` must identify technical, integration, testing, and delivery risks.",
                *self._todo_breakdown_schema_rules(),
            ]
        if context.role == "reviewer":
            if context.phase == "PLAN_REVIEW":
                return [
                    "- This is the planning merge-review phase after planner peer-review loops.",
                    "- `review_report.md` and `selected_plan.md` must each contain `artifact_result_code: 0` when complete.",
                    "- Merge the current-round planner `plan.md`, `assumptions.md`, `risk.md`, `todo_breakdown.md`, and `peer_review.md` artifacts into one authoritative executor plan.",
                    "- Do not merely pick one planner proposal when other proposals contain useful compatible details; preserve the strongest compatible requirements, risks, acceptance criteria, and test commands.",
                    "- If planner proposals conflict materially or peer review still contains blocking objections, write `review_decision_code: 1` and explain the required planning revision instead of inventing a compromise.",
                    "- `selected_plan.md` is the single authoritative plan for executor agents.",
                    "- `selected_plan.md` must include files, steps, acceptance criteria, test commands, dependencies, and risks using the planner todo schema.",
                    "- `review_report.md` must summarize which planner artifacts were merged, any discarded conflicting points, and include one machine-readable line: `review_decision_code: 0` when the merged plan is actionable enough for execution, `review_decision_code: 1` when changes are required, or `review_decision_code: -1` for blocking rejection.",
                    "- `review_decision_code` is the merge-review verdict. It must not be copied into the `delivery.md` return code.",
                ]
            return [
                "- `review_report.md` must contain `artifact_result_code: 0` when complete.",
                "- Treat `selected_plan.md` as the authoritative customer requirement and acceptance baseline for this review phase.",
                "- Review `merged_patch.diff` as the authoritative implementation artifact whenever it exists.",
                "- Review `merged_patch_metadata.md` as required baseline/apply-target evidence for the authoritative patch.",
                "- Treat raw `patch.diff` and `fix_patch.diff` as background candidates only, not as the delivered implementation.",
                "- Use `runtime_readiness.md` as Harness-provided environment evidence when present.",
                "- `review_report.md` must include one machine-readable line: `review_decision_code: 0` for approved, `review_decision_code: 1` for changes required, or `review_decision_code: -1` for blocking rejection.",
                "- `review_decision_code` is the review verdict. It must not be copied into the `delivery.md` return code.",
                "- Review correctness, scope control, regressions, security, maintainability, and customer-machine runtime readiness.",
                "- If this is a code or runnable-project delivery, run the repository on this machine and verify the delivered environment actually works before approving.",
                "- When `runtime_readiness.md` exists, inspect it first; do not repeat identical setup/test commands unless evidence is incomplete or suspicious.",
                "- You may create isolated local runtime state such as `.venv`, package caches, or installed dependencies inside the repository workspace only as needed for verification.",
                "- Record every command you used for environment setup and runtime verification inside `review_report.md`.",
                "- `review_report.md` must include a `## Review Verdict JSON` section with exactly one fenced `json` object.",
                '- The JSON object must include: `{"review_status":"approved|changes_required|blocked","environment_check":{"attempted":true,"status":"ready|changes_required|blocked|not_applicable","commands_run":["..."],"fixable":true,"blocking_reason":""}}`.',
                "- Use `environment_check.status: changes_required` for fixable setup/runtime issues and include concrete remediation steps in `review_report.md`.",
                "- Use `environment_check.status: blocked` only for irreconcilable runtime or system conflicts. When you use it, set `review_status: blocked`, set `review_decision_code: -1`, and write the exact blocking reason.",
                "- When changes are required, include concrete fix instructions and affected artifacts.",
            ]
        if context.role == "communicator":
            return [
                "- `final_delivery.md` and `usage_guide.md` must each contain `artifact_result_code: 0` when complete.",
                "- `final_delivery.md` must summarize final outcome code, completed work, the accepted plan, the concrete implementation, and known risks.",
                "- `final_delivery.md` must include one machine-readable line: `final_delivery_code: 0` for accepted final delivery, `final_delivery_code: 1` for partial delivery, `final_delivery_code: 2` for blocked delivery, or `final_delivery_code: -1` for failed delivery.",
                "- `final_delivery.md` must include a short handoff section with exactly these fields: `project_dir`, `run_command`, and `dependency_install`.",
                "- `project_dir` must point to the delivered source/project directory when available, not merely to Harness internal artifact files.",
                "- `run_command` must be the exact command the user should execute from the project directory.",
                "- Use `selected_plan.md` plus the final executor artifacts as your primary sources of truth. Do not pad the customer handoff with judge chatter, gate reports, or internal retry history.",
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
        specializations_by_role = self._specializations_for_context(context)
        if not specializations_by_role:
            return []

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

    def _specializations_for_context(self, context: AgentRunContext) -> dict[int, list[tuple[str, str, list[str]]]] | None:
        if context.role == "planner":
            workflow_type = str(context.metadata.get("workflow_type") or context.config.get("workflow_type") or "")
            if workflow_type == BUGFIX:
                return BUGFIX_PLANNER_SPECIALIZATIONS
            if workflow_type == FEATURE_CHANGE:
                return FIX_MODIFY_PLANNER_SPECIALIZATIONS
            return PLANNER_SPECIALIZATIONS
        if context.role == "tester":
            return TESTER_SPECIALIZATIONS
        return None

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

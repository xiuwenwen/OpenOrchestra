from __future__ import annotations


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
                "Define the contents of final_delivery.json.",
                "Make the result understandable.",
                "Define acceptance criteria.",
                "Document known limitations.",
                "Ensure the completed system is usable by the user.",
            ],
        ),
    ],
}

FIX_MODIFY_PLANNER_SPECIALIZATIONS: dict[int, list[tuple[str, str, list[str]]]] = {
    1: [
        (
            "Balanced Fix/Modify Planner",
            "Balanced brownfield planning, covering root-cause analysis, impact control, minimal-change strategy, regression risk, and validation planning.",
            [
                "Classify the task as bug_fix, behavior_modify, refactor, feature_extension, or unknown.",
                "Understand the existing project before proposing changes.",
                "Identify the suspected root cause or change point.",
                "Control modification scope and avoid unnecessary refactoring.",
                "Document impact analysis, non-goals, validation strategy, and regression risk.",
                "Produce Executor-ready todo items with todo_id, title, description, suggested_owner, affected_paths, dependencies, and validation_hint.",
                "Record missing information instead of guessing; do not modify code, run tests, or ask the user directly.",
            ],
        )
    ],
    2: [
        (
            "Minimal Patch Planner",
            "Minimal-change planning, root-cause-oriented, implementation-scoped, regression-minimizing.",
            [
                "Include Task Type Classification in plan.md.",
                "Focus on the exact issue or requested behavior change.",
                "Identify the most likely root cause or behavior change point.",
                "List files/modules likely to change and files/modules that should not change.",
                "Plan the smallest safe diff and explicitly exclude unnecessary changes.",
                "Avoid redesign, new frameworks, large refactors, unrelated modules, and code edits.",
                "Give small-grained todo items with target files/modules and validation method for each todo.",
            ],
        ),
        (
            "Regression Risk Planner",
            "Regression-risk analysis, compatibility preservation, impact-aware planning, safety-first modification.",
            [
                "Identify existing behaviors, modules, interfaces, state, config, and data flows that could be affected.",
                "Surface hidden dependencies, backward compatibility risks, state contamination risks, and test blind spots.",
                "Define dangerous changes Executor must avoid.",
                "Define regression scenarios Tester must verify and before/after comparison points.",
                "Keep the plan compatibility-preserving and avoid unnecessary large changes.",
            ],
        ),
    ],
    3: [
        (
            "Root Cause Planner",
            "Root-cause analysis, behavior tracing, defect localization, causality-first planning.",
            [
                "Compare current and expected behavior and list trigger conditions.",
                "Trace relevant code paths, data flow, control flow, and state changes.",
                "Identify the most likely defect location and evidence needed to confirm it.",
                "Split todo items into investigation, modification, and validation tasks with reasoning.",
                "Avoid superficial workarounds, scope expansion, code edits, and direct user questions.",
            ],
        ),
        (
            "Minimal Change Planner",
            "Minimal-diff strategy, scope control, implementation efficiency, low-risk modification.",
            [
                "Separate strictly required changes from optional or forbidden changes.",
                "Minimize diff size and Executor error risk.",
                "Define implementation boundaries, affected paths, forbidden paths, validation hints, and rollback considerations.",
                "Avoid large refactors, unrelated abstractions, unrelated modules, and code edits.",
            ],
        ),
        (
            "Regression & Compatibility Planner",
            "Regression protection, backward compatibility, integration safety, behavioral stability.",
            [
                "Preserve existing APIs, CLI behavior, configuration, data formats, and file structure unless explicitly changed.",
                "Identify cross-module integration risks, state/data migration risks, and test coverage gaps.",
                "Define required regression tests, compatibility validation items, integration validation items, and Tester focus areas.",
                "Do not validate only the new behavior.",
            ],
        ),
    ],
    4: [
        (
            "Diagnostic Planner",
            "Diagnostic clarity, root-cause localization, behavior tracing, evidence-driven planning.",
            [
                "Clarify current behavior, expected behavior, trigger conditions, suspected root cause, and relevant code paths.",
                "List evidence to collect and blocking unknowns.",
                "Give diagnostic, localization, minimal-fix, and validation todo items.",
                "Do not jump to large implementation or refactoring.",
            ],
        ),
        (
            "Minimal Patch Planner",
            "Minimal patching, strict scope control, low-risk implementation, rollback-friendly planning.",
            [
                "Define the minimum required changes, forbidden changes, affected paths, non-affected paths, and Executor guardrails.",
                "Avoid interface, data structure, dependency, and architecture changes unless strictly required.",
                "Include expected_output and validation_hint for each todo.",
            ],
        ),
        (
            "Impact & Regression Planner",
            "Impact analysis, regression-risk control, compatibility preservation, integration awareness.",
            [
                "Identify direct and indirect impact areas, compatibility requirements, regression-sensitive paths, and integration risks.",
                "Define required regression coverage, Tester focus areas, and safe rollout considerations.",
                "Do not allow compatibility-breaking changes unless explicitly requested.",
            ],
        ),
        (
            "Verification Planner",
            "Verification-driven planning, acceptance clarity, testability, evidence-based completion criteria.",
            [
                "Define acceptance criteria, success/failure conditions, required tests, regression tests, edge cases, and evidence required.",
                "Give Tester instructions and final delivery explanation notes.",
                "Avoid vague 'run tests' guidance and make user acceptance measurable.",
            ],
        ),
    ],
}

BUGFIX_PLANNER_SPECIALIZATIONS: dict[int, list[tuple[str, str, list[str]]]] = {
    1: [
        (
            "Root Cause Bugfix Planner",
            "Root-cause-first brownfield bugfix planning with strict scope control and executable validation.",
            [
                "Classify the reported defect and separate symptom, trigger, current behavior, and expected behavior.",
                "Trace the likely code path from user-facing symptom through parsing/control/data flow to the defect location.",
                "Identify the smallest safe repair and explicitly reject surface-only fixes that only silence the reported exception.",
                "Define concrete executor todos with affected paths, forbidden paths, evidence needed, and rollback notes.",
                "Define a minimal reproduction or smoke scenario that proves the fixed behavior end to end.",
                "Record blocking unknowns instead of guessing; do not modify code, run tests, or ask the user directly.",
            ],
        )
    ],
    2: [
        (
            "Root Cause Minimal Fix Planner",
            "Minimal, causality-driven bugfix planning focused on the exact defect and the smallest safe code path change.",
            [
                "Reconstruct current versus expected behavior from the user request and repository evidence.",
                "Identify the most likely root cause, not just the first error message or missing parameter.",
                "Map the repair to specific files/functions and list files that should not change.",
                "Specify executor tasks that include investigation checkpoints before code edits.",
                "Require a proof scenario that exercises the full behavior after the patch, not only syntax or import success.",
                "Keep the patch small, reversible, and compatible with existing behavior.",
            ],
        ),
        (
            "Verification & Regression Bugfix Planner",
            "Validation-first bugfix planning covering runnable tests, regression risk, edge cases, and acceptance evidence.",
            [
                "Define exactly how Tester should prove the bug is fixed, including commands, inputs, expected outputs, and failure signals.",
                "Identify existing tests, likely targeted tests, smoke checks, and minimal manual reproductions when full tests are expensive.",
                "List regression-sensitive behavior, compatibility expectations, and edge cases the fix must not break.",
                "State that runnable tests or smoke checks must be run when the repository provides them and the environment allows it.",
                "If automated tests may be blocked, define the smallest executable reproduction script or command Tester should try first.",
                "Call out validation blind spots that should cause Tester or Reviewer to reject static-only approval.",
            ],
        ),
    ],
    3: [
        (
            "Diagnostic Bugfix Planner",
            "Diagnostic clarity, reproduction, root-cause localization, and evidence-driven repair planning.",
            [
                "Clarify the reported symptom, trigger conditions, expected behavior, and likely affected code paths.",
                "List evidence to collect before editing and mark any unknowns that block safe repair.",
                "Define diagnostic, localization, minimal-fix, and validation todo items.",
                "Do not jump directly to broad refactoring or unrelated implementation.",
            ],
        ),
        (
            "Minimal Patch Bugfix Planner",
            "Minimal diff, strict scope boundaries, rollback-friendly implementation, and compatibility preservation.",
            [
                "Define the minimum required changes, forbidden changes, affected paths, and non-affected paths.",
                "Avoid interface, data structure, dependency, and architecture changes unless required by the defect.",
                "Give executor guardrails and validation hints for each todo.",
            ],
        ),
        (
            "Regression Validation Bugfix Planner",
            "Regression protection, edge-case coverage, integration safety, and tester acceptance criteria.",
            [
                "Identify existing behavior that must remain unchanged and regression-sensitive paths.",
                "Define runnable test, smoke, and reproduction commands when discoverable from repository files.",
                "Define acceptance criteria and failure conditions precise enough for Tester to reject incomplete fixes.",
            ],
        ),
    ],
    4: [
        (
            "Reproduction Planner",
            "Create a precise reproduction and localization plan before repair.",
            [
                "Define current behavior, expected behavior, trigger inputs, and the smallest reproduction route.",
                "Identify code paths and evidence needed to prove the root cause.",
                "Produce investigation and validation todo items.",
            ],
        ),
        (
            "Root Cause Planner",
            "Trace causality from symptom to defect and avoid shallow exception-only fixes.",
            [
                "Analyze data flow, control flow, and state changes related to the failure.",
                "Define likely defect locations and confirmation checks.",
                "Separate root-cause repair from workarounds.",
            ],
        ),
        (
            "Minimal Patch Planner",
            "Keep the fix small, reviewable, rollback-friendly, and compatible.",
            [
                "List required, optional, and forbidden changes.",
                "Define affected paths, non-affected paths, and executor guardrails.",
                "Avoid unrelated refactoring and new dependencies.",
            ],
        ),
        (
            "Verification Planner",
            "Define proof, regression coverage, edge cases, and acceptance evidence.",
            [
                "List required tests, smoke checks, edge cases, expected outputs, and failure conditions.",
                "Require runnable validation when project files expose runnable commands.",
                "Identify validation blind spots that should block approval.",
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
                "Do not infer success from implementation shape alone.",
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

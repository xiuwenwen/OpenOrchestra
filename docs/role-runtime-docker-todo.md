# Docker Runtime And Role Boundary Todo

## Objective

Make OpenOrchestra a host-side control plane with a Docker-backed execution plane. Harness keeps workflow state, artifact storage, routing, visibility, and UI on the host. Agent role subprocesses and command-based gates run through the resolved runtime when `runtime.mode=docker`.

## Architecture Rule

- Host control plane: config loading, state DB, artifact registry, workspace creation, visibility staging, routing, progress events, UI, and delivery publishing.
- Docker execution plane: planner/executor/tester/reviewer/communicator subprocesses, patch-gate command checks, final-validation commands, test/runtime command execution.
- Artifact boundary: roles exchange versioned artifacts only; they do not mutate Harness state directly.
- Role boundary: every role has one owner contract for source access, command authority, environment ownership, and deliverables.
- Routing boundary: Harness routes from structured status/taxonomy fields, not free-form prose.

## P0: Runtime Coverage

- [x] Runtime dataclasses: `RuntimeSpec`, `PathMapping`, `RuntimeCommandRequest`, `RuntimeCommandResult`.
- [x] Runtime resolver from config/env/CLI: `runtime.mode`, image, workdir, network, cache, env allowlist.
- [x] Split Docker network policy by execution boundary: role agent API access, test setup, test execution, patch gate, and final validation.
- [x] Host runtime executor with process-group timeout cleanup.
- [x] Docker runtime executor with container create/start/exec/rm lifecycle.
- [x] `CommandRunner.run_capture()` can delegate to Docker when `runtime_spec.is_docker`.
- [x] `SubprocessRunner.run()` can delegate to Docker while preserving stdout/stderr logs.
- [x] Agent role subprocesses receive runtime path mapping for repo/input/output/logs.
- [x] Docker runtime invocation evidence is written for subprocess roles.
- [x] Patch gate passes resolved runtime into git apply/materialize/diff-check commands.
- [x] Patch gate uses runtime-local patch copies so Docker commands do not depend on host-only patch paths.
- [x] Final validation passes resolved runtime into external/final evaluator commands.
- [x] Final validation maps `{repo_dir}` and `{external_evaluator_log_dir}` to container paths under Docker.
- [x] Final validation records runtime mode/image/workdir/log path in `external_evaluator_result.json`.

## P0: Role Boundary Contract

- [x] Add a central role-boundary contract with role ownership, source access, command authority, environment ownership, and forbidden actions.
- [x] Render the role boundary into every agent prompt.
- [x] Make executor boundary explicit: source changes only; no environment verdict ownership.
- [x] Make tester boundary explicit: owns environment/test loop; no source modification.
- [x] Make reviewer boundary explicit: read-only implementation review and runtime-readiness evidence; no source modification.
- [x] Make planner boundary explicit: plan/contracts only; no source modification.
- [x] Make communicator boundary explicit: final handoff only; no implementation/test changes.

## P1: Hard Isolation Follow-Up

- [ ] Enforce role boundary mechanically before artifact collection, not only in prompts.
- [ ] Reject role outputs that write forbidden artifact types for the current phase.
- [ ] Add role-specific filesystem mount policy:
  - executor: writable repo, writable output/logs.
  - tester: read-only source plus writable isolated runtime state where possible.
  - reviewer: read-only source plus writable isolated verification state where possible.
  - planner/communicator: read-only source, writable output/logs.
- [ ] Store runtime invocation evidence for `CommandRunner.run_capture()` gates, not only subprocess runner logs.
- [ ] Add a single command bus abstraction so future gates cannot bypass runtime by calling `subprocess` directly.
- [ ] Add CI guardrail that flags direct `subprocess.Popen/run/check_output` outside runtime/host executor modules.

## P1: Routing And Taxonomy

- [x] Keep tester environment blockers with tester instead of executor.
- [x] Route only source/behavior bugs into executor fix loops.
- [x] Allow no-op fix artifacts when current repository is already correct.
- [ ] Add a route matrix fixture that covers tester, reviewer, patch gate, runtime readiness, and final validation together.
- [ ] Include raw command stderr/stdout excerpts in structured routing artifacts for every gate failure.

## P2: Operational Defaults

- [x] Make Docker the default role runtime; `host` is available only by explicit configuration.
- [x] Make Docker the default test runtime; `native` is available only by explicit configuration.
- [ ] Document the operational command for choosing a Docker image: `--runtime docker --runtime-docker-image <image>`.
- [x] Add a preflight that verifies the configured Docker image has git, Python, shell, selected agent CLI, and backend API network reachability.
- [ ] Add a first-party OpenOrchestra runtime image build/check command.

## Acceptance Criteria

- With no runtime override, Harness resolves `runtime.mode=docker` and `testing.runtime=docker`.
- With `runtime.mode=docker`, agent subprocesses, patch gate, test/runtime commands, and final validation all execute through Docker runtime.
- Role agent containers use an explicit backend-capable network; test setup/test execution/gates use their own explicit network settings.
- A gate cannot accidentally run host `python`, host `git`, or host test commands under the default runtime.
- Every role prompt includes a clear role boundary section.
- Environment/setup failures remain in tester-owned loops unless a structured gate marks an infra/blocking condition.

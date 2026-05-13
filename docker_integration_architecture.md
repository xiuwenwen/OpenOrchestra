# Docker Integration Architecture Plan

## Executive Decision

Docker should be embedded as Harness-owned execution infrastructure, not as a role prompt responsibility.

The correct integration boundary is:

```text
PatchGate
-> MaterializedRepo
-> TestExecutionGate
-> Tester
-> Judge
-> RuntimeReadinessGate
-> Reviewer
```

Tester and Reviewer consume structured evidence. They should not design Docker images, install dependencies ad hoc, or infer success from source shape.

## Fit With Current Architecture

This project already has useful bounded contexts:

- `harness/gates/`: hard gates such as patch and test validation.
- `harness/workflow/`: phase orchestration.
- `harness/context/`: role input staging.
- `harness/artifacts/`: contracts, visibility, validation, and persistence.
- `harness/materialization/`: runnable repository snapshots.

Docker fits best as a new testing execution boundary under `harness/testing/`, with gates depending on it. It should not be placed in `harness/prompts/`, `harness/agents/`, or role-specific logic.

## Corrections To The Initial Proposal

The earlier plan was directionally right but too broad in a few places:

1. Do not create microservices or external daemons.
   This is a local orchestration tool. In-process service classes are enough.

2. Do not add a generic Docker system for every role first.
   Start with TestExecutionGate and RuntimeReadinessGate.

3. Do not let Tester or Reviewer run arbitrary Docker setup from prompt instructions.
   Harness should execute Docker and write evidence; roles interpret evidence.

4. Do not use Docker Sandboxes or `sbx` as the default.
   Use stable Docker CLI first. Docker Sandboxes are useful later for agent isolation, but they are not the first testing gate.

5. Do not overfit SWE-bench into the core.
   SWE-bench should be a specialized runner behind the same `TestRunner` interface.

## Target Bounded Contexts

### Test Execution

Owner:

```text
harness/testing/
```

Responsibility:

- Detect project test environment.
- Select native, Docker, or SWE-bench runner.
- Execute commands with timeout and resource policy.
- Produce structured evidence and logs.

Public API:

```python
class TestRunner:
    def run(self, request: TestRunRequest) -> TestRunEvidence:
        ...
```

### Runtime Readiness

Owner:

```text
harness/gates/runtime_readiness.py
```

Responsibility:

- Verify final delivered project can be installed, started, or smoke-tested in a clean environment.
- Feed Reviewer with environment evidence.
- Distinguish fixable setup issues from irreconcilable system conflicts.

### Evidence Contract

Owner:

```text
harness/testing/evidence.py
```

Responsibility:

- Define the single schema consumed by Tester, Reviewer, Judge, UI, and diagnostics.
- Avoid natural-language-only test conclusions.

## Recommended File Layout

```text
harness/testing/
  __init__.py
  detection.py
  evidence.py
  config.py
  runners/
    __init__.py
    base.py
    native.py
    docker.py
    swebench.py

harness/gates/
  test_gate.py
  runtime_readiness.py
```

## Evidence Schema

```json
{
  "runtime": "native|docker|swebench",
  "image": "",
  "project_type": "python|node|go|rust|dockerfile|unknown",
  "environment_status": "pass|fail|blocked|skipped",
  "build_status": "pass|fail|blocked|skipped",
  "test_status": "pass|fail|blocked|skipped",
  "failure_type": "none|env_setup|build|test|business|timeout|infra",
  "commands": [
    {
      "name": "test",
      "command": "python -m pytest -q",
      "exit_code": 0,
      "stdout": "path/to/stdout.log",
      "stderr": "path/to/stderr.log"
    }
  ],
  "cache_key": "",
  "notes": []
}
```

## Docker Selection Policy

### SWE-bench

Use SWE-bench official Docker evaluation whenever the task is identified as a SWE-bench instance.

Reason:

- SWE-bench already owns the correct environment.
- It avoids local Python version drift.
- It produces benchmark-valid results.

### Python Projects

Default image:

```text
python:3.11-bookworm
```

Detection priority:

```text
.python-version
pyproject.toml requires-python
tox.ini
setup.cfg
runtime.txt
fallback: python:3.11-bookworm
```

Do not default to `slim`; older projects often need compiler/system libraries.

### Node Projects

Default image:

```text
node:20-bookworm
```

Detection:

```text
package.json
package-lock.json
pnpm-lock.yaml
yarn.lock
```

### Project-Provided Containers

Priority:

```text
Dockerfile
.devcontainer/devcontainer.json
docker-compose.yml
```

Use project-provided Docker only when it is present and safe to build.

## Docker Security Policy

Default Docker command policy:

```text
--rm
--user <current uid>:<current gid>
--workdir /workspace
-v <materialized_repo>:/workspace:rw
-v <harness_cache>:/cache:rw
--network none
```

Forbidden by default:

```text
--privileged
-v /:/host
-v /var/run/docker.sock:/var/run/docker.sock
host network
host pid
host ipc
```

Network policy:

```text
none          default for test execution
install_only  allowed only during dependency installation
always        explicit opt-in only
```

## Config Additions

```yaml
testing:
  runtime: docker
  docker:
    enabled: true
    default_python_image: python:3.11-bookworm
    default_node_image: node:20-bookworm
    network: install_only
    timeout_seconds: 1800
    cache_mounts: true
    allow_project_dockerfile: true
```

CLI overrides:

```text
--test-runtime auto|native|docker|swebench
--test-docker-image IMAGE
--no-docker-test
--docker-network none|install_only|always
```

Environment variables:

```text
OO_TEST_RUNTIME
OO_TEST_DOCKER_IMAGE
OO_TEST_DOCKER_NETWORK
OO_TEST_TIMEOUT_SECONDS
```

## Tester Integration

Tester should receive:

- `manifest.md`
- current round `test_gate.md`
- structured test evidence path
- log paths
- materialized repository path

Tester should not receive broad executor narrative artifacts.

Tester prompt rules:

```text
Use Harness Test Gate evidence as primary execution evidence.
Do not reinstall dependencies if Test Gate evidence is complete.
Do not declare a fix correct when build or test execution is blocked.
Do not infer success from implementation shape alone.
If evidence is incomplete, document exactly what is missing.
```

Tester output remains:

```text
bug_report.md
delivery.md
```

## Reviewer Integration

Reviewer should receive:

- accepted plan
- final authoritative implementation evidence
- runtime readiness evidence
- selected deliverable paths

Reviewer should not re-run the full test suite unless RuntimeReadinessGate evidence is missing or suspicious.

Reviewer verdict JSON should include:

```json
{
  "review_status": "approved|changes_required|blocked",
  "environment_check": {
    "attempted": true,
    "runtime": "docker|native|swebench",
    "image": "",
    "status": "ready|changes_required|blocked|not_applicable",
    "commands_run": [],
    "fixable": true,
    "blocking_reason": ""
  }
}
```

## Judge Integration

Judge should not run Docker.

Judge should decide from:

- `objective_gate.md`
- `test_gate.md`
- `bug_report.md`
- `runtime_readiness.md` when reviewing final delivery

Hard rule:

```text
Structured gate evidence beats natural-language role reports.
```

## UI Integration

Show execution evidence as first-class state:

```text
Runtime: docker/native/swebench
Image: python:3.11-bookworm
Environment: pass/fail/blocked
Build: pass/fail/blocked/skipped
Test: pass/fail/blocked/skipped
Failure type: env_setup/build/test/business/timeout/infra
Logs: stdout/stderr links
```

This prevents users from reading `OUTPUT_INVALID` or `FAILED` as a vague test failure.

## Implementation Order

### Phase 1: No Behavior Change

- Add `harness/testing/runners/base.py`.
- Add `NativeTestRunner`.
- Move current `TestGateService` command execution into `NativeTestRunner`.
- Keep current tests green.

### Phase 2: Evidence Contract

- Add `TestRunEvidence`.
- Make `test_gate.md` render from structured evidence.
- Add `evidence.json` beside logs.
- Update cache key to include runtime and image.

### Phase 3: Docker Runner

- Add `DockerTestRunner`.
- Add Docker availability check.
- Add Python and Node detection.
- Add timeout, log capture, cache mount, and network policy.
- Add tests for command construction without requiring real Docker for unit tests.

### Phase 4: Tester Prompt And Visibility

- Stage current-round `test_gate.md` and evidence path to Tester.
- Keep Tester inputs lean.
- Add blocked-test rule: blocked build/test cannot become bug success.

### Phase 5: Runtime Readiness Gate

- Add `RuntimeReadinessGate`.
- Run it before final Reviewer for code deliveries.
- Feed readiness evidence to Reviewer.

### Phase 6: SWE-bench Runner

- Add `SweBenchDockerRunner`.
- Use official SWE-bench evaluation for identified SWE-bench instances.
- Keep this behind the same evidence schema.

### Phase 7: UI And Diagnostics

- Expose runtime/test/build/failure type in UI.
- Add `/diagnose` output for Docker evidence and log paths.

## Test Plan

Unit tests:

```text
tests/test_testing_detection.py
tests/test_native_test_runner.py
tests/test_docker_test_runner.py
tests/test_test_gate_evidence.py
tests/test_runtime_readiness_gate.py
tests/test_tester_prompt_docker_evidence.py
tests/test_reviewer_runtime_readiness.py
```

Fixture projects:

```text
tests/fixtures/python_pass_project
tests/fixtures/python_fail_tests
tests/fixtures/python_build_fail
tests/fixtures/node_pass_project
tests/fixtures/dockerfile_project
```

Integration checks:

```text
pytest -q
python3 run.py --limit 1 --backend claude --ui --evaluate
```

## Main Risks

1. Docker startup/build time slows every loop.
   Mitigation: runtime `auto`, cache key, native fallback for simple projects.

2. Docker evidence becomes too large for prompts.
   Mitigation: store logs as files; stage summaries and paths only.

3. Docker security drift.
   Mitigation: central command builder, denylisted flags, tests for forbidden mounts.

4. SWE-bench logic pollutes normal workflows.
   Mitigation: keep SWE-bench behind `SweBenchDockerRunner`, not in workflow engine.

5. Tester reverts to static implementation guessing.
   Mitigation: prompt rules plus evidence schema; blocked execution cannot become success.

## Final Architecture Rule

Docker is a Harness-controlled execution substrate.

Roles consume evidence. Gates execute commands. Workflow decides progression. Artifacts preserve proof.

## Implementation Status

Implemented in this repository:

- `harness/testing/` runner boundary.
- Native runner with structured evidence.
- Docker runner with no-shell command execution, project Dockerfile build support, cache mount, network policy, timeout, and log capture.
- Evidence JSON embedded in gate artifacts and written beside logs.
- Runtime readiness gate before final reviewer.
- Tester and Reviewer prompt/contract rules that consume gate evidence instead of guessing from source shape.
- UI progress labels for test gate and runtime readiness failures.
- CLI and env overrides for runtime, Docker image, Docker network, and test timeout.

Kept intentionally external:

- Official SWE-bench evaluation remains driven by the SWE-bench runner project. Harness exposes a compatible runner hook and evidence schema, but normal project validation must not depend on benchmark-specific workflow code.

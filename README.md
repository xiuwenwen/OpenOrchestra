# Harness System MVP

This project implements a minimal orchestration harness for mature coding agents such as Codex CLI or Claude Code.

The harness does not implement internal tools like FileTool, ShellTool, EditTool, or TestTool. Agents are responsible for reading files, editing code, and running commands inside their own isolated workspace. The harness only coordinates phases, retries, timeouts, workspace isolation, artifact collection, artifact validation, judge decisions, and final delivery.

## What is included

- SQLite state store for tasks, phases, agent runs, artifacts, and judge decisions.
- Local filesystem workspace store under `workspaces/`.
- Local filesystem artifact store under `artifacts/`.
- `MockAgentAdapter` for tests only.
- Placeholder `CodexCLIAdapter` and `ClaudeCodeAdapter` using `subprocess`.
- Required output validation by role and phase.
- Versioned artifact collection with SHA-256 hashes.
- A mock judge and communicator-driven final delivery.
- Pytest coverage for the state store, workspace isolation, artifact validation, mock adapter, and full mock flow.

## Run the real agent flow

Interactive mode:

```bash
.venv/bin/python -m harness.main
```

Then type a task prompt at `harness>`. Use `exit` to quit.

Interactive commands:

```text
/backend                 Show current backend
/use claude              Switch all roles to Claude Code
/use codex               Switch all roles to Codex CLI
/history [n]             List recent tasks
/resume <n|task_id>      Select a historical task as context
/continue                Continue/retry the selected historical task
/clean                   Remove selected task workspaces/artifacts; keep success_path
/current                 Show selected historical context
/clear                   Clear selected historical context
/ui                      Start/show the local Web execution viewer
/help                    Show command help
```

Interactive command input uses `prompt_toolkit` for live completion. It supports
real-time command candidates while typing, Tab completion, left/right cursor
movement, up/down command history, and Chinese input methods without the old
custom character-by-character redraw loop. `/history` prints numbered tasks with
status, phase, result type, created time, and prompt summary so `/resume <n>` is
easier to choose.

When stdout is a TTY, task execution uses a live terminal dashboard. It shows:

- current task status, phase, and backend
- test/fix round and review round
- per-role status for planner, executor, tester, reviewer, judge, communicator
- current agent, attempt, artifact count, and recent progress events
- final delivery path when available

When stdout is not a TTY, the CLI falls back to line-oriented progress logs.

For a richer view, start the local Web execution viewer:

```bash
.venv/bin/python -m harness.main --ui
.venv/bin/python -m harness.main --ui --backend claude "做一个简单工具"
```

In interactive mode, `/ui` starts the viewer on demand and prints the local URL.
By default it listens on `http://127.0.0.1:8765`.

The Web viewer shows:

- task status, workflow, phases, role status, and agent attempts
- live progress events and heartbeat events
- each role's visible prompt, `stdout.log`, and `stderr.log`
- produced artifacts such as `plan.md`, `merged_patch.diff`, `test_report.md`,
  `review_report.md`, `decision.json`, and final delivery files
- a role delivery browser where one or more roles can be selected, then filtered
  by round to inspect that role's visible reasoning trail and delivered md/json
  artifacts
- each role/round/attempt's full prompt in `logs/prompt.md`, exposed from the
  role delivery browser as "完整提示词" / "full prompt"
- a Chinese/English UI toggle. In Chinese mode, prompt text, role delivery
  prose, and visible model output are translated with a local glossary while
  file paths, commands, URLs, code blocks, JSON/config lines, and diffs remain
  unchanged; switch back to English to inspect the original text.

The viewer displays only observable agent output and files. It does not expose
hidden model chain-of-thought; use `stdout.log`, reports, and artifacts as the
agent's visible reasoning and delivery evidence.

After `/resume`, the prompt changes to include the selected task id prefix, for
example `harness[claude task=8748b388]>`. The next non-command task you type
starts a new Harness task, but the selected historical final delivery and recent
judge decisions are included as reference context, similar to resuming context in
Claude/Codex while keeping Harness task records immutable.

One-shot mode:

```bash
.venv/bin/python -m harness.main "实现一个简单任务"
```

If `--workflow` is not provided, Harness asks the selected backend model to
classify the prompt before execution. The model must return one of:

```text
bugfix
feature_change
new_project
misc
```

For project workflows, Harness prints the selected workflow and passes it to the
orchestrator. For `misc`, Harness prints only the model answer.

You can also choose the workflow explicitly:

```bash
.venv/bin/python -m harness.main --workflow bugfix "修复登录失败的问题"
.venv/bin/python -m harness.main --workflow feature_change "给现有应用增加导出功能"
.venv/bin/python -m harness.main --workflow new_project "做一个根据 IP 查询天气的软件"
.venv/bin/python -m harness.main --workflow misc "解释一下当前 dashboard 的含义"
```

During execution, the CLI prints live progress events for the current phase,
role, agent, retry attempt, and completion status.

`misc` requests are answered directly by the selected backend model. They do not
create a Harness task, do not open the execution dashboard, and do not write
artifacts.

The CLI uses a real backend by default. `--backend auto` prefers `codex`, then
`claude`. You can force one:

```bash
.venv/bin/python -m harness.main --backend codex "实现一个简单任务"
.venv/bin/python -m harness.main --backend claude "实现一个简单任务"
```

The command finally prints:

- `task_id`
- for project workflows, the shallow `deliver/<ascii-project-name>-<task-number>/final_delivery.md` path
- for project workflows, `success_path`, the shallow delivery directory containing the final delivery, usage guide, manifest, patch, and copied supporting artifacts
- `task_workspace`, the stable workspace root for the task; `/continue` reuses
  the same task id and keeps follow-up role runs under this root
- the `usage_guide.md` path in the same delivery directory when produced
- for the `misc` workflow, the direct model answer is printed instead

Use `/clean` after `/resume <n|task_id>` to remove intermediate
`workspaces/<task_id>` and `artifacts/<task_id>` files for that task. Harness
refuses to clean unless it can find the final `success_path`, and it leaves the
published `deliver/<ascii-project-name>-<task-number>/` directory intact.
Delivery directory names use an ASCII-safe slug. A Chinese-only prompt falls
back to `project-<task-number>` instead of using the prompt text as a path name.

By default it loads built-in defaults from `config/config.yaml`, then writes and
reads user-level overrides from `~/.myharness.env`. For day-to-day use, edit
`~/.myharness.env`.

## Run tests

```bash
.venv/bin/python -m pytest
```

If `.venv` does not exist yet:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## Configuration

`~/.myharness.env` is the user-facing configuration file. On startup, Harness
loads defaults from `config/config.yaml`, ensures missing keys exist in
`~/.myharness.env`, then applies `~/.myharness.env` over the defaults.

Use:

```bash
cat ~/.myharness.env
```

`config/config.yaml` remains the project default template. You normally do not
need to edit it.

Set any role timeout to `0` to disable timeout enforcement for that role. The
default config uses `0` for all roles, so Claude/Codex can keep working until it
exits; heartbeat events still show that the role is alive.

Example `~/.myharness.env` keys:

```env
HARNESS_BACKEND=claude
HARNESS_WORKSPACE_ROOT=./workspaces
HARNESS_ARTIFACT_ROOT=./artifacts
HARNESS_DELIVER_ROOT=./deliver
HARNESS_STATE_DB=./state/harness.db
HARNESS_SOURCE_REPO=.
HARNESS_PLANNER_COUNT=2
HARNESS_EXECUTOR_COUNT=2
HARNESS_TESTER_COUNT=1
HARNESS_REVIEWER_COUNT=1
HARNESS_JUDGE_COUNT=1
HARNESS_COMMUNICATOR_COUNT=1
HARNESS_TIMEOUT_PLANNER=0
HARNESS_TIMEOUT_EXECUTOR=0
HARNESS_UI_HOST=127.0.0.1
HARNESS_UI_PORT=8765
HARNESS_PLANNING_PEER_REVIEW_LOOPS=3
HARNESS_CLAUDE_MAX_TOKENS_PLANNER=128000
HARNESS_CLAUDE_MAX_TOKENS_EXECUTOR=64000
```

Command-line flags still take precedence, so `--backend codex` overrides the
saved value for that invocation.

It only serves files under Harness-controlled roots: `workspaces/`,
`artifacts/`, `deliver/`, and `logs/`.

Harness respects configured role counts by default. The default config runs two
planner agents, two executor agents, one tester, one reviewer, one judge, and one
communicator. Roles still run serially across phases, but agents inside the same
role phase run concurrently when `policy.same_role_can_run_concurrently` is true
and the role count is greater than one. Use `--serial-agents` to force planner,
executor, tester, and reviewer counts to one for a single run.

Project/history background is not copied into every role prompt. When `/resume`
is active, Harness writes historical context as `project_context.md`, stages it
as an input artifact, and each role receives only the artifacts relevant to its
phase. Parallel agents in the same phase do not see each other's outputs.

Internal artifacts remain versioned under `artifacts/`, but final user-facing
deliverables are copied to a shallow directory:

```text
deliver/<ascii-project-name>-<task-number>/final_delivery.md
deliver/<ascii-project-name>-<task-number>/usage_guide.md
deliver/<ascii-project-name>-<task-number>/success_path.md
deliver/<ascii-project-name>-<task-number>/artifacts_manifest.md
deliver/<ascii-project-name>-<task-number>/patches/final.patch
deliver/<ascii-project-name>-<task-number>/artifacts/
deliver/<ascii-project-name>-<task-number>/source/
```

`<ascii-project-name>` is derived from ASCII words in the prompt. If the prompt
does not contain ASCII words, Harness uses `project`.

`system.source_repo` identifies the local repository to copy into agent
workspaces for `bugfix` and `feature_change` workflows. The default is `.`.
Generated directories such as `workspaces/`, `artifacts/`, `deliver/`, `state/`,
`logs/`, `.venv/`, and `.git/` are ignored during workspace copy. `new_project`
workflows still start from an empty repo workspace.

`source/` is populated when Harness can safely materialize files from the final
unified diff. New files are reconstructed directly. Modified files are
reconstructed against `system.source_repo` when the base file exists. The final
patch is always published as `patches/final.patch`.

`patches/final.patch` is copied from `merged_patch.diff`, not directly from an
arbitrary executor output. `PATCH_MERGE` is a single-agent executor phase: the
model reads candidate `patch.diff` / `fix_patch.diff` artifacts, produces the
authoritative `merged_patch.diff`, and records selected, rejected, or adjusted
candidate artifacts in `merge_report.md`. Tester, reviewer, judge, and
communicator phases receive the merged patch as their authoritative
implementation artifact.

If the workflow classifier fails to return JSON, Harness only treats the raw
model output as a direct `misc` answer for clearly informational prompts. For
project-building or project-changing prompts, classifier failure is surfaced
instead of silently skipping the Harness workflow.

Classifier and direct `misc` model calls run inside their own log directories
instead of the Harness project root. Harness does not force a Codex sandbox mode
for these calls; Codex uses the user's own configured sandbox policy. Harness
also does not force Claude `--permission-mode`; set `claude.permission_mode` in
config only if you explicitly want to pass one.

Harness overrides Claude Code's output-token reservation for role execution via
`claude.max_output_tokens`. This prevents a large global Claude setting such as
`CLAUDE_CODE_MAX_OUTPUT_TOKENS=200000` from reserving most of a 200k context
window and causing `input_tokens + max_tokens` overflow. The config may be a
single integer or a per-role mapping:

```yaml
claude:
  max_output_tokens:
    classifier: 2048
    misc: 168000
    planner: 128000
    executor: 64000
    tester: 64000
    reviewer: 128000
    judge: 128000
    communicator: 64000
```

Set a `HARNESS_CLAUDE_MAX_TOKENS_*` value to `0` to stop Harness from overriding
your environment for that role.

Agent heartbeat events are emitted while a role is running. The default interval
is configured in `~/.myharness.env`:

```env
HARNESS_HEARTBEAT_INTERVAL_SECONDS=60
```

The default backend for every role is `codex`:

```yaml
agent_backend:
  default: "codex"
  planner: "codex"
  executor: "codex"
  tester: "codex"
  reviewer: "codex"
  judge: "codex"
  communicator: "codex"
```

## Adapter contract

Every adapter receives an `AgentRunContext` with:

- task and phase identifiers
- role and agent id
- isolated `workspace/input`, `workspace/output`, `workspace/logs`, and `workspace/repo` paths
- input artifact paths
- required output filenames
- timeout and config

Every adapter returns an `AgentRunResult`. The orchestrator then validates required files in `workspace/output`, collects them into the artifact store, records hashes and versions, and advances the state machine.

Every role and every phase must also write `delivery.md` into `workspace/output`.
Harness treats it as the agent's explicit status report and validates it before
accepting the run:

```markdown
# Role Delivery

status: success

role: executor
phase: EXECUTION
summary: Produced implementation artifacts.
known_risks: none
```

Allowed status values are `success`, `failed`, and `partial`. Only `success`
allows the agent run to be accepted. `failed` or `partial` keeps the run from
advancing even if the process exits with code 0 and the other required files
exist.

## Connecting Codex CLI or Claude Code later

The placeholders live in:

- `harness/adapters/codex_cli_adapter.py`
- `harness/adapters/claude_code_adapter.py`
- `harness/adapters/subprocess_runner.py`

To wire a real backend:

1. Set `HARNESS_BACKEND=codex` or `HARNESS_BACKEND=claude` in `~/.myharness.env`, or use `/use codex` / `/use claude`.
2. Adjust the adapter command construction for the installed CLI.
3. Ensure the CLI writes all required files into `context.output_dir`.
4. Keep stdout/stderr captured under `context.log_dir`.
5. Let the mature agent use its own tools inside `context.repo_dir`; do not add internal harness tools.

The communicator role must produce both:

- `final_delivery.md`: final outcome summary, status, evidence, risks, success path, source/project path when available, and exact next-run commands.
- `usage_guide.md`: practical usage instructions for the delivered result, including prerequisites, setup, run commands, configuration, verification, common failure modes, and artifact locations.

## MVP state machine

Harness now supports three bounded workflows.

### New project

Used for a fresh project. Planning uses a configurable planner peer-review loop
before execution. Set `HARNESS_PLANNING_PEER_REVIEW_LOOPS` or
`limits.planning_peer_review_loops`; the default is 3.

```text
CREATED
PLANNING_DRAFT
loop planning_peer_review_loops:
  PLANNING_PEER_REVIEW
  PLANNING_REVISION when peer review requests changes
PLAN_REVIEW
PLAN_JUDGEMENT
EXECUTION
PATCH_MERGE
TESTING
TEST_JUDGEMENT
REVIEWING
REVIEW_JUDGEMENT
FINAL_JUDGEMENT
DELIVERY
COMPLETED
```

### Bug fix

Used when the prompt is a repair request. It skips full planning and repeats
issue fix plus testing until the judge accepts the test artifacts:

```text
CREATED
loop max_test_fix_rounds:
  FIXING
  PATCH_MERGE
  TESTING
  TEST_JUDGEMENT
FINAL_JUDGEMENT
DELIVERY
COMPLETED
```

### Feature change

Used when adding or modifying behavior in an existing project. The planner first
checks compatibility and blast radius, then execution and testing run as a
bounded loop. Review-required changes are followed by executor fixes and a
regression test/fix loop before returning to review:

```text
CREATED
PLANNING_DRAFT
loop planning_peer_review_loops:
  PLANNING_PEER_REVIEW
  PLANNING_REVISION when peer review requests changes
PLAN_REVIEW
PLAN_JUDGEMENT
EXECUTION
PATCH_MERGE
loop max_test_fix_rounds:
  TESTING
  TEST_JUDGEMENT
  FIXING when tests fail
loop max_review_rounds:
  REVIEWING
  REVIEW_JUDGEMENT
  REVIEW_FIXING when review requires changes
  loop max_test_fix_rounds:
    REGRESSION_TESTING
    TEST_JUDGEMENT
    REVIEW_FIXING when regression fails
FINAL_JUDGEMENT
DELIVERY
COMPLETED
```

All loops are bounded by values loaded from `~/.myharness.env`. The mock judge passes by default,
so fix loops are only entered when a real judge or future policy returns failure
or changes required.

### Miscellaneous response

Used for questions, explanations, analysis, advice, or other requests that do
not ask Harness to create, modify, or fix project files. The prompt and selected
historical context are passed directly to the selected backend model:

```text
classify prompt -> misc
direct model response
return to interactive prompt
```

`misc` does not create phases, agent runs, artifacts, or dashboard output. The
answer is printed in the terminal and the interactive session continues.

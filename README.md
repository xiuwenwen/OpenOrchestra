# OpenOrchestra

OpenOrchestra implements a minimal orchestration harness for mature coding agents such as Codex CLI or Claude Code.

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

## Quick Start / 快速开始

### 1. Clone the repository / 克隆仓库

```bash
git clone git@github.com:xiuwenwen/OpenOrchestra.git
cd OpenOrchestra
```

If you use HTTPS instead of SSH:

如果你使用 HTTPS：

```bash
git clone https://github.com/xiuwenwen/OpenOrchestra.git
cd OpenOrchestra
```

### 2. Create a Python environment / 创建 Python 环境

OpenOrchestra requires Python 3.11 or newer.

OpenOrchestra 需要 Python 3.11 或更高版本。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

### 3. Prepare an agent backend / 准备 Agent 后端

Install and authenticate at least one supported mature coding agent:

安装并登录至少一个支持的成熟编码 Agent：

- Claude Code: make sure `claude` is available in `PATH`.
- Codex CLI: make sure `codex` is available in `PATH`.

OpenOrchestra can select a backend automatically, or you can force one with
`--backend claude` or `--backend codex`.

OpenOrchestra 可以自动选择后端，也可以用 `--backend claude` 或
`--backend codex` 指定。

### 4. Start OpenOrchestra / 启动 OpenOrchestra

```bash
./orchestra
```

The Web execution viewer starts by default and prints a local URL, usually:

默认会启动 Web 执行查看器，并打印本地地址，通常是：

```text
http://127.0.0.1:8765
```

Then type a task in the interactive prompt:

然后在交互式命令行里输入任务：

```text
harness[claude]> 做一个根据 IP 查询天气的小工具
```

### 5. Run a one-shot task / 一次性执行任务

```bash
./orchestra --backend claude "做一个根据 IP 查询天气的小工具"
./orchestra --backend codex "fix the failing login test"
```

To skip automatic workflow classification, pass the workflow explicitly:

如果不想让模型自动分类工作流，可以显式指定：

```bash
./orchestra --workflow new_project "做一个待办事项 CLI"
./orchestra --workflow bugfix "修复现有项目里的登录失败问题"
./orchestra --workflow feature_change "给现有应用增加 CSV 导出"
./orchestra --workflow misc "解释一下这个项目怎么运行"
```

### 6. Read the result / 查看结果

For project workflows, OpenOrchestra prints:

对于项目类工作流，OpenOrchestra 最后会打印：

- `project_dir`: delivered project directory / 交付工程目录
- `run_command`: command to run or verify the project / 运行或验证命令
- `dependency_install`: dependency install command, or `not_required` / 依赖安装命令，或 `not_required`

You can also inspect the live process and artifacts in the Web viewer.

你也可以在 Web 查看器里查看实时流程、角色输出和交付产物。

### 7. Run tests / 运行测试

```bash
.venv/bin/python -m pytest
```

## Run the real agent flow

Interactive mode:

```bash
./orchestra
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

The local Web execution viewer starts by default:

```bash
./orchestra
./orchestra --backend claude "做一个简单工具"
```

In interactive mode, `/ui` shows or starts the viewer and prints the local URL.
By default it listens on `http://127.0.0.1:8765`. Use `--no-ui` to run without
the viewer:

```bash
./orchestra --no-ui
```

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
./orchestra "实现一个简单任务"
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
./orchestra --workflow bugfix "修复登录失败的问题"
./orchestra --workflow feature_change "给现有应用增加导出功能"
./orchestra --workflow new_project "做一个根据 IP 查询天气的软件"
./orchestra --workflow misc "解释一下当前 dashboard 的含义"
```

During execution, the CLI prints live progress events for the current phase,
role, agent, retry attempt, and completion status.

`misc` requests are answered directly by the selected backend model. They do not
create a Harness task, do not open the execution dashboard, and do not write
artifacts.

The CLI uses a real backend by default. `--backend auto` prefers `codex`, then
`claude`. You can force one:

```bash
./orchestra --backend codex "实现一个简单任务"
./orchestra --backend claude "实现一个简单任务"
```

The command finally prints:

- `project_dir`, the delivered project source directory
- `run_command`, the command to start or verify the delivered project
- `dependency_install`, the dependency installation command, or `not_required`
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

Set any role timeout to `0` to disable timeout enforcement for that role. Use a
positive timeout in day-to-day Claude/Codex runs so provider or gateway hangs do
not leave Harness waiting forever; heartbeat events show that the role is still
alive while the process runs.

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
HARNESS_TIMEOUT_PLANNER=1800
HARNESS_TIMEOUT_EXECUTOR=3600
HARNESS_UI_HOST=127.0.0.1
HARNESS_UI_PORT=8765
HARNESS_PLANNING_PEER_REVIEW_LOOPS=3
HARNESS_CLAUDE_CONTEXT_WINDOW_TOKENS=200000
HARNESS_CLAUDE_CONTEXT_WINDOW_BUFFER_TOKENS=2048
HARNESS_CLAUDE_MAX_TOKENS_PLANNER=64000
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
`claude.max_output_tokens`. This controls the output reservation only; it does
not shrink the input prompt or model-side conversation. For a 200k context
window, `input_tokens + max_output_tokens` must still fit. Harness also uses
`claude.context_window_tokens` to lower the per-request output reservation when
the estimated prompt size would exceed the configured context window. The local
estimate is conservative but not identical to provider tokenization, so request
diagnostics still handle provider-side context errors.

The config may be a single integer or a per-role mapping:

```yaml
claude:
  context_window_tokens: 200000
  context_window_buffer_tokens: 2048
  max_output_tokens:
    classifier: 2048
    misc: 64000
    planner: 64000
    executor: 64000
    tester: 64000
    reviewer: 64000
    judge: 64000
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
Harness treats it as the agent's explicit numeric return-code report and validates it before
accepting the run:

```markdown
return_code: 0

# Role Delivery

role: executor
phase: EXECUTION
role_return_code: 0
summary: Produced implementation artifacts.
known_risks: none
```

The first non-empty line must be `return_code: <integer>`. `return_code: 0`
allows the agent run to be accepted. Non-zero return codes keep the run from
advancing even if the process exits with code 0 and the other required files
exist. Business verdicts belong in role-specific artifacts such as
`decision.json`, `test_report.md`, `review_report.md`, or `peer_review.md`, not
in `delivery.md`.

Canonical return codes are defined in `harness/artifacts/delivery_codes.py` and
used by both prompt generation and artifact validation:

| Code | Meaning |
| ---: | --- |
| `0` | Role delivery files are complete and Harness may accept the role run. |
| `1` | Partial role delivery; useful files may exist but the contract is incomplete. |
| `2` | Blocked by missing input, context, or evidence required to complete the role. |
| `3` | Degraded role delivery that requires manual review before it can be trusted. |
| `-1` | Role failed to produce a usable result. |
| `-2` | Required role outputs are missing, empty, or invalid. |
| `-3` | Tool, runtime, adapter, or internal execution error. |

Every other required Markdown artifact must also start with a numeric result
code:

```markdown
artifact_result_code: 0

# Test Report

test_result_code: -1
evidence: pytest failed in tests/test_login.py
```

`artifact_result_code` describes whether that Markdown file itself is complete.
Business verdicts inside Markdown files must use numeric `*_code` fields such
as `test_result_code`, `build_result_code`, `bug_result_code`,
`review_decision_code`, `peer_review_code`, `decision_code`, and
`final_delivery_code`. Do not use Markdown verdict fields like
`test_result: pass`, `review_decision: approved`, or
`peer_review_status: satisfied`.

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

# OpenOrchestra

OpenOrchestra 是一个面向成熟编码 Agent 的本地编排器。它协调 Codex CLI、Claude Code 等 Agent 完成规划、执行、测试、审查、裁决和交付。

OpenOrchestra is a local orchestration harness for mature coding agents such as Codex CLI and Claude Code. It coordinates planning, execution, testing, review, judgement, and delivery.

OpenOrchestra 不内置文件编辑、Shell、测试等工具。Agent 在隔离工作区里使用自己的工具完成读写和命令执行；OpenOrchestra 负责阶段流转、重试、超时、日志、artifact 校验、patch gate、judge gate 和最终交付。

OpenOrchestra does not implement internal file, shell, edit, or test tools. Agents work inside isolated workspaces with their own tools; OpenOrchestra handles phase control, retries, timeouts, logs, artifact validation, patch gates, judge gates, and final delivery.

## Features / 功能

- 任务、阶段、Agent run、artifact 和 judge decision 使用 SQLite 记录。
- Isolated `workspaces/`, versioned `artifacts/`, and published `deliver/` outputs.
- 支持 `codex`、`claude` 和测试用 `mock` backend。
- Web UI 默认启动，展示实时任务流、流程循环、角色状态、stdout/stderr 和交付文件。
- PATCH_MERGE 产物经过 hard gate：unified diff 格式、scope、`git apply --check`、`git diff --check`、敏感路径和异常大小检查。
- Judge 使用结构化证据，不直接相信自然语言测试结论。
- 所有角色交付使用统一 numeric return code。

## Quick Start / 快速开始

### 1. Clone / 克隆

```bash
git clone git@github.com:xiuwenwen/OpenOrchestra.git
cd OpenOrchestra
```

HTTPS:

```bash
git clone https://github.com/xiuwenwen/OpenOrchestra.git
cd OpenOrchestra
```

### 2. Install / 安装

OpenOrchestra requires Python 3.11 or newer.

OpenOrchestra 需要 Python 3.11 或更高版本。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Make sure at least one real backend is installed and authenticated:

确保至少安装并登录一个真实 backend：

- Claude Code: `claude` must be available in `PATH`.
- Codex CLI: `codex` must be available in `PATH`.

### 3. Run / 运行

默认命令会启动交互模式、Web UI 和实时输出。

The default command starts interactive mode, the Web UI, and live progress output.

```bash
./orchestra
```

The UI usually listens on:

UI 通常监听：

```text
http://127.0.0.1:8765
```

Then type a task:

然后输入任务：

```text
harness[claude]> 做一个根据 IP 查询天气的小工具
```

Run a one-shot task:

一次性执行任务：

```bash
./orchestra --backend claude "做一个根据 IP 查询天气的小工具"
./orchestra --backend codex "fix the failing login test"
```

Choose a workflow explicitly:

显式指定工作流：

```bash
./orchestra --workflow new_project "做一个待办事项 CLI"
./orchestra --workflow bugfix "修复现有项目里的登录失败问题"
./orchestra --workflow feature_change "给现有应用增加 CSV 导出"
./orchestra --workflow misc "解释一下这个项目怎么运行"
```

### 4. Read Results / 查看结果

Project workflows print these user-facing fields:

项目类工作流会打印这些面向用户的字段：

- `project_dir`: delivered project directory / 交付工程目录
- `run_command`: command to run or verify the project / 运行或验证命令
- `dependency_install`: dependency install command, or `not_required` / 依赖安装命令，或 `not_required`

Final files are published under:

最终文件发布到：

```text
deliver/<project-name>-<task-number>/
```

Common files:

常见文件：

```text
final_delivery.md
usage_guide.md
success_path.md
artifacts_manifest.md
patches/final.patch
source/
artifacts/
```

### 5. Test / 测试

```bash
.venv/bin/python -m pytest
```

## Interactive Commands / 交互命令

```text
/backend                 Show current backend / 查看当前 backend
/use claude              Switch all roles to Claude Code / 切换到 Claude Code
/use codex               Switch all roles to Codex CLI / 切换到 Codex CLI
/history [n]             List recent tasks / 查看历史任务
/resume <n|task_id>      Select a historical task as context / 选择历史任务作为上下文
/continue                Continue or retry the selected task / 继续或重试选中任务
/clean                   Remove intermediate workspace/artifact files / 清理中间文件
/current                 Show selected context / 查看当前上下文
/clear                   Clear selected context / 清空上下文
/ui                      Start or show the Web UI / 启动或显示 Web UI
/help                    Show help / 查看帮助
```

After `/resume`, the prompt includes the selected task id. New non-command input starts a new task with historical context. `/continue` retries or continues the selected historical task.

执行 `/resume` 后，提示符会带上选中的 task id。新的非命令输入会创建新任务并携带历史上下文；`/continue` 会继续或重试选中的历史任务。

## Configuration / 配置

OpenOrchestra loads defaults from `config/config.yaml`, then applies user overrides from `~/.myharness.env`. Command-line flags take precedence for the current run.

OpenOrchestra 先读取 `config/config.yaml` 默认值，再读取 `~/.myharness.env` 用户配置。命令行参数只覆盖当前运行。

Common settings:

常用配置：

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
HARNESS_TIMEOUT_EXECUTOR=3600
HARNESS_UI_HOST=127.0.0.1
HARNESS_UI_PORT=8765
HARNESS_MAX_TEST_FIX_ROUNDS=unlimited
HARNESS_PLANNING_PEER_REVIEW_LOOPS=3
HARNESS_HEARTBEAT_INTERVAL_SECONDS=60
HARNESS_CLAUDE_CONTEXT_WINDOW_TOKENS=200000
HARNESS_CLAUDE_CONTEXT_WINDOW_BUFFER_TOKENS=2048
HARNESS_CLAUDE_MAX_TOKENS_EXECUTOR=64000
```

Set a role timeout to `0` to disable timeout enforcement for that role. Use positive timeouts for real agent runs so provider hangs do not block forever.

把角色 timeout 设为 `0` 可以关闭该角色的超时限制。真实 Agent 运行建议使用正数超时，避免 provider 卡住后无限等待。

`HARNESS_MAX_TEST_FIX_ROUNDS=unlimited` means test/fix loops keep running until the test judge passes or the task is stopped. A positive integer restores a bounded loop.

`HARNESS_MAX_TEST_FIX_ROUNDS=unlimited` 表示测试/修复循环会一直运行，直到测试裁决通过或任务被停止。设置为正整数可以恢复有界循环。

## Workflows / 工作流

OpenOrchestra supports four workflow types.

OpenOrchestra 支持四类工作流。

### `new_project`

用于从零创建项目。流程包含规划互审、执行、patch merge、测试、审查、最终裁决和交付。

Used to create a project from scratch. The flow includes planning peer review, execution, patch merge, testing, review, final judgement, and delivery.

```text
PLANNING_DRAFT
PLANNING_PEER_REVIEW / PLANNING_REVISION loop
PLAN_REVIEW
PLAN_JUDGEMENT
EXECUTION
PATCH_MERGE
TESTING / TEST_JUDGEMENT / FIXING loop
REVIEWING / REVIEW_JUDGEMENT / REVIEW_FIXING loop
FINAL_JUDGEMENT
DELIVERY
```

### `bugfix`

用于修复已有项目。它跳过完整规划，只在修复、patch merge、测试和测试裁决之间循环。

Used to repair an existing project. It skips full planning and loops over fixing, patch merge, testing, and test judgement.

```text
FIXING
PATCH_MERGE
TESTING
TEST_JUDGEMENT
FINAL_JUDGEMENT
DELIVERY
```

### `feature_change`

用于修改已有项目或增加功能。它先做规划和兼容性检查，再进入执行、测试、审查和回归验证。

Used to modify an existing project or add a feature. It plans compatibility and blast radius first, then executes, tests, reviews, and runs regression checks.

### `misc`

用于解释、分析、建议等不需要创建或修改项目文件的请求。它直接调用选中的 backend，不创建 Harness 阶段和 artifact。

Used for explanations, analysis, and advice that do not create or modify project files. It calls the selected backend directly and does not create Harness phases or artifacts.

## Artifact Contract / 交付契约

Every role must write `delivery.md`. Its first non-empty line must be a numeric return code:

每个角色都必须写入 `delivery.md`。第一个非空行必须是 numeric return code：

```markdown
return_code: 0

# Role Delivery

role: executor
phase: EXECUTION
role_return_code: 0
summary: Produced implementation artifacts.
```

Return code meanings:

返回值含义：

| Code | Meaning |
| ---: | --- |
| `0` | Role delivery files are complete and Harness may accept the role run. |
| `1` | Partial role delivery; useful files may exist but the contract is incomplete. |
| `2` | Blocked by missing input, context, or evidence. |
| `3` | Degraded role delivery that requires manual review. |
| `-1` | Role failed to produce a usable result. |
| `-2` | Required outputs are missing, empty, or invalid. |
| `-3` | Tool, runtime, adapter, or internal execution error. |

Every required Markdown artifact must also start with:

每个必需 Markdown artifact 也必须以此开头：

```markdown
artifact_result_code: 0
```

Business verdicts must use numeric fields such as `test_result_code`, `review_decision_code`, `peer_review_code`, `decision_code`, and `final_delivery_code`. Do not use natural-language verdict fields as the delivery status.

业务判断必须使用 numeric 字段，例如 `test_result_code`、`review_decision_code`、`peer_review_code`、`decision_code` 和 `final_delivery_code`。不要用自然语言字段表达交付状态。

## Patch Gate / 补丁门禁

`PATCH_MERGE` produces the authoritative `merged_patch.diff`. Harness validates it before tester, reviewer, judge, or communicator can trust it.

`PATCH_MERGE` 产出权威 `merged_patch.diff`。Harness 会先验证它，再交给 tester、reviewer、judge 和 communicator。

The gate checks:

门禁检查：

- legal unified diff format / 合法 unified diff 格式
- allowed changed files / 修改范围
- `git apply --check`
- `git diff --check`
- abnormal size and mass deletion / 异常大小和大量删除
- sensitive paths such as `.git`, `.env`, keys, and tokens / 敏感路径

For new projects, Harness can materialize a `source/` directory from the final patch. For existing-project workflows, `system.source_repo` provides the baseline.

新项目可以从最终 patch 物化 `source/` 目录。已有项目的 `bugfix` 和 `feature_change` 使用 `system.source_repo` 作为基线。

## Development / 开发

Run all tests:

运行全部测试：

```bash
.venv/bin/python -m pytest
```

Useful directories:

常用目录：

```text
harness/adapters/        Agent backend adapters
harness/core/            Orchestration and workflow logic
harness/prompts/         Prompt builder and templates
harness/patch/           Patch hard gate
harness/ui/              Local Web UI
tests/                   Test suite
```

Generated runtime directories such as `workspaces/`, `artifacts/`, `deliver/`, `state/`, `logs/`, and `workPlaces/` should not be committed.

运行生成目录，例如 `workspaces/`、`artifacts/`、`deliver/`、`state/`、`logs/` 和 `workPlaces/`，不应该提交到 git。

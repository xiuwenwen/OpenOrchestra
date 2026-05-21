# OpenOrchestra

[![CI](https://github.com/xiuwenwen/OpenOrchestra/actions/workflows/ci.yml/badge.svg)](https://github.com/xiuwenwen/OpenOrchestra/actions/workflows/ci.yml)

OpenOrchestra is an artifact-mediated orchestration kernel for coding agents. Agents collaborate through versioned artifacts, objective gates, and structured decisions rather than direct free-form chat.

OpenOrchestra 是一个基于 artifact 的 Coding Agent 协作编排内核。

## Quick Start / 快速开始

### 1. Install / 安装

```bash
git clone git@github.com:xiuwenwen/OpenOrchestra.git
cd OpenOrchestra
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e .
```

OpenOrchestra requires Python 3.11 or newer.

确保至少安装并登录一个真实 backend：

- Claude Code: `claude` must be available in `PATH`.
- Codex CLI: `codex` must be available in `PATH`.
- Gemini CLI: `gemini` must be available in `PATH`.
- Qwen CLI: `qwen` must be available in `PATH`; non-interactive runs may require `qwen.auth_type`.

### 2. Start Interactive Mode / 启动交互模式

```bash
./orchestra
```

The default command starts interactive mode, the Web UI, and live progress output. The UI usually listens on:

```text
http://127.0.0.1:8765
```

然后直接输入任务：

```text
harness[claude]> 做一个根据 IP 查询天气的小工具
```

### 3. Run One-Shot Tasks / 一次性执行任务

```bash
./orchestra --backend claude "做一个根据 IP 查询天气的小工具"
./orchestra --backend codex "fix the failing login test"
./orchestra --backend gemini "解释一下这个项目怎么运行"
./orchestra --backend qwen "review this repository"
```

### 4. Choose A Workflow / 指定工作流

```bash
./orchestra --workflow new_project "做一个待办事项 CLI"
./orchestra --workflow bugfix "修复现有项目里的登录失败问题"
./orchestra --workflow feature_change "给现有应用增加 CSV 导出"
./orchestra --workflow misc "解释一下这个项目怎么运行"
```

### 5. Read Results / 查看结果

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
final_delivery.json
usage_guide.md
success_path.md
artifacts_manifest.md
patches/final.patch
source/
artifacts/
```

### 6. Test OpenOrchestra / 测试工程自身

```bash
.venv/bin/python -m pytest
```

## Project Highlights / 工程特色

OpenOrchestra coordinates mature coding agents such as Codex CLI, Claude Code, Gemini CLI, and Qwen CLI. Agents do not collaborate through direct free-form chat; they collaborate through versioned artifacts, objective gates, and structured decisions.

OpenOrchestra 面向成熟编码 Agent，例如 Codex CLI、Claude Code、Gemini CLI、Qwen CLI。Agent 不直接聊天，而是通过版本化产物、客观门禁和结构化裁决协作。

OpenOrchestra does not implement internal file, shell, edit, or test tools. Agents work inside isolated workspaces with their own tools; OpenOrchestra handles phase control, retries, timeouts, logs, artifact validation, patch gates, structured routing, and final delivery.

OpenOrchestra 不内置文件编辑、Shell、测试等工具。Agent 在隔离工作区里使用自己的工具完成读写和命令执行；OpenOrchestra 负责阶段流转、重试、超时、日志、artifact 校验、patch gate、结构化路由和最终交付。

- 任务、阶段、Agent run 和 artifact 使用 SQLite 记录。
- Isolated `workspaces/`, versioned `artifacts/`, and published `deliver/` outputs.
- 支持 `codex`、`claude`、`gemini`、`qwen` 和测试用 `mock` backend。
- Web UI 默认启动，展示实时任务流、流程循环、角色状态、stdout/stderr 和交付文件。
- PATCH_MERGE 产物经过 hard gate：unified diff 格式、scope、`git apply --check`、`git diff --check`、敏感路径和异常大小检查。
- Tester/reviewer 使用结构化证据和路由码，不直接相信自然语言测试结论。
- 所有角色交付使用统一 numeric return code。

## Interactive Commands / 交互命令

```text
/backend                 Show current backend / 查看当前 backend
/use claude              Switch all roles to Claude Code / 切换到 Claude Code
/use codex               Switch all roles to Codex CLI / 切换到 Codex CLI
/use gemini              Switch all roles to Gemini CLI / 切换到 Gemini CLI
/use qwen                Switch all roles to Qwen CLI / 切换到 Qwen CLI
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

OpenOrchestra loads defaults from `config/config.yaml`, then applies user overrides from `~/.openorchestra.env`. For compatibility, existing `~/.myharness.env` files and `HARNESS_*` keys are still read as legacy aliases. Command-line flags take precedence for the current run.

OpenOrchestra 先读取 `config/config.yaml` 默认值，再读取 `~/.openorchestra.env` 用户配置。为了兼容，已有的 `~/.myharness.env` 文件和 `HARNESS_*` key 仍会作为旧别名读取。命令行参数只覆盖当前运行。

Common settings:

常用配置：

```env
OO_BACKEND=claude
OO_WORKSPACE_ROOT=./workspaces
OO_ARTIFACT_ROOT=./artifacts
OO_DELIVER_ROOT=./deliver
OO_STATE_DB=./state/harness.db
OO_SOURCE_REPO=.
OO_RUNTIME=docker
OO_RUNTIME_DOCKER_IMAGE=openorchestra-agent-runtime:latest
OO_RUNTIME_DOCKER_NETWORK=bridge
OO_TEST_RUNTIME=docker
OO_TEST_DOCKER_SETUP_NETWORK=bridge
OO_TEST_DOCKER_TEST_NETWORK=none
OO_PLANNER_COUNT=2
OO_EXECUTOR_COUNT=2
OO_TESTER_COUNT=1
OO_REVIEWER_COUNT=1
OO_COMMUNICATOR_COUNT=1
OO_TIMEOUT_EXECUTOR=3600
OO_UI_HOST=127.0.0.1
OO_UI_PORT=8765
OO_MAX_TEST_FIX_ROUNDS=10
OO_PLANNING_PEER_REVIEW_LOOPS=3
OO_PLANNER_PEER_REVIEW_DIFFICULTY_THRESHOLD=5
OO_HEARTBEAT_INTERVAL_SECONDS=60
OO_CLAUDE_CONTEXT_WINDOW_TOKENS=200000
OO_CLAUDE_CONTEXT_WINDOW_BUFFER_TOKENS=2048
OO_CLAUDE_MAX_TOKENS_EXECUTOR=64000
```

For Qwen Code, configure a supported auth provider in `config/config.yaml` when the CLI requires it:

Qwen Code 如需非交互认证，请在 `config/config.yaml` 里配置可用认证方式：

```yaml
qwen:
  auth_type: "openai"
  openai_base_url: "https://api.example.com/v1"
```

Set a role timeout to `0` to disable timeout enforcement for that role. Use positive timeouts for real agent runs so provider hangs do not block forever.

把角色 timeout 设为 `0` 可以关闭该角色的超时限制。真实 Agent 运行建议使用正数超时，避免 provider 卡住后无限等待。

`OO_MAX_TEST_FIX_ROUNDS=10` is the default guardrail for test/fix loops. When the limit is reached, interactive mode asks whether to add 10 more rounds, exit, or continue until fixed. Set `OO_MAX_TEST_FIX_ROUNDS=unlimited` only when you intentionally want an unbounded loop.

`OO_MAX_TEST_FIX_ROUNDS=10` 是测试/修复循环的默认保护上限。达到上限后，交互模式会询问额外给 10 轮、退出，或一直修复直到通过。只有明确需要无界循环时才设置 `OO_MAX_TEST_FIX_ROUNDS=unlimited`。

## Internal Flow / 内部流程

OpenOrchestra separates user intent, workflow routing, agent execution, testing, review, patch validation, and final delivery. The workflow engine decides the phase sequence; role contracts define each agent's required artifacts; gates consume structured JSON results instead of free-form claims.

OpenOrchestra 将用户意图、流程路由、Agent 执行、测试、审查、patch 校验和最终交付拆开处理。Workflow engine 决定阶段顺序；role contract 定义每个 Agent 必须交付哪些产物；gate 消费结构化 JSON 结果，而不是自然语言判断。

### Test Runtime Boundary / 测试运行边界

OpenOrchestra itself runs on the host: orchestration, UI, state, artifacts, and Docker daemon probing stay on the local machine. Agent backend CLIs, patch gates, final validation commands, project setup, and test commands run in Docker when the corresponding runtime selects Docker.

OpenOrchestra 主进程运行在宿主机：编排、UI、状态库、artifacts、Docker daemon 检测都在本机。Agent backend CLI、patch gate、final validation、项目依赖安装和测试命令在对应 runtime 选择 Docker 时都进入 Docker。

Role runtime Docker network and test runtime Docker networks are separate. Role agents usually need outbound API access for Codex/Claude/Gemini/Qwen, so `OO_RUNTIME_DOCKER_NETWORK=bridge` is the normal default. Test setup can use `OO_TEST_DOCKER_SETUP_NETWORK=bridge`; actual tests can run with `OO_TEST_DOCKER_TEST_NETWORK=none` for stricter isolation.

Role runtime Docker 网络和 test runtime Docker 网络是两套配置。Role Agent 通常需要访问 Codex/Claude/Gemini/Qwen API，所以默认使用 `OO_RUNTIME_DOCKER_NETWORK=bridge`。测试环境安装阶段可以用 `OO_TEST_DOCKER_SETUP_NETWORK=bridge`，真正测试阶段可以用 `OO_TEST_DOCKER_TEST_NETWORK=none` 做更强隔离。

When `testing.runtime` selects Docker, the source repo is mounted as `/workspace` and generated test commands use container commands such as:

当 `testing.runtime` 选择 Docker 时，源码仓库会挂载为 `/workspace`，自动生成的测试命令使用容器内命令，例如：

```bash
python -m pytest -q
python -m compileall -q .
```

Host Python paths such as `/Users/.../.venv/bin/python` are invalid inside Docker. If a tester-provided Docker setup/test command leaks a host path, Harness records it as an environment/test-command gate failure and routes it back through tester environment repair instead of sending the task back into another source-code fixing round.

宿主机 Python 路径，例如 `/Users/.../.venv/bin/python`，不能进入 Docker 命令。如果 tester 提供的 Docker setup/test command 泄漏宿主机路径，Harness 会把它记录为环境/测试命令门禁失败，并回到 tester 修复环境，而不是继续让 Agent 修改源码。

### Workflows / 工作流

OpenOrchestra supports four workflow types.

OpenOrchestra 支持四类工作流。

#### `new_project`

用于从零创建项目。流程包含规划互审、方案合并审阅、执行、patch merge、测试、审查、必要的审查修复/回归测试和交付。

Used to create a project from scratch. The flow includes planning peer review, plan merge review, execution, patch merge, testing, reviewer verdict, optional review fixes/regression testing, and delivery.

```text
PLANNING_DRAFT
PLANNING_PEER_REVIEW / PLANNING_REVISION loop
PLAN_REVIEW
EXECUTION
PATCH_MERGE
TESTING -> TESTING environment repair loop when tester_result.json reports environment_dependency_issue=true
TESTING -> FIXING loop when tester_result.json reports source_bug and environment_dependency_issue=false
REVIEWING / REVIEW_FIXING / REGRESSION_TESTING loop when tester_result.json reports source_bug and environment_dependency_issue=false
DELIVERY
```

#### `bugfix`

用于修复已有项目。它跳过完整规划，先在修复、patch merge 和测试之间循环，通过后进入审查、必要的审查修复/回归测试和交付。

Used to repair an existing project. It skips full planning, loops over fixing, patch merge, and testing, then runs reviewer verdict handling before delivery.

```text
FIXING
PATCH_MERGE
TESTING -> TESTING environment repair loop when tester_result.json reports environment_dependency_issue=true
TESTING -> FIXING loop when tester_result.json reports source_bug and environment_dependency_issue=false
REVIEWING / REVIEW_FIXING / REGRESSION_TESTING loop when tester_result.json reports source_bug and environment_dependency_issue=false
DELIVERY
```

Tester owns test-environment repair and command execution. It must write `tester_result.json` with `environment_dependency_issue: true | false` and `status: tests_passed | source_bug | environment_blocked`; Harness checks `environment_dependency_issue` before `status`, keeps environment problems in the tester repair loop, and does not run a separate post-tester test gate.

tester 负责测试环境修复和命令执行，必须写入 `tester_result.json`，其中 `environment_dependency_issue` 表示环境依赖是否仍有问题，状态只能是 `tests_passed | source_bug | environment_blocked`；Harness 先消费 `environment_dependency_issue`，环境问题留在 tester 修复 loop，不再在 tester 后单独跑 test gate。

#### `feature_change`

用于修改已有项目或增加功能。它先做规划和兼容性检查，再进入执行、测试、审查和回归验证。

Used to modify an existing project or add a feature. It plans compatibility and blast radius first, then executes, tests, reviews, and runs regression checks.

#### `misc`

用于解释、分析、建议等不需要创建或修改项目文件的请求。它直接调用选中的 backend，不创建 Harness 阶段和 artifact。

Used for explanations, analysis, and advice that do not create or modify project files. It calls the selected backend directly and does not create Harness phases or artifacts.

### Artifact Contract / 交付契约

Every role must write `delivery.md`. It is a JSON role return envelope, not the business verdict:

每个角色都必须写入 `delivery.md`。它是 JSON 角色返回信封，不是业务判断：

```json
{
  "return_code": 0,
  "task_status": "success",
  "role_return_code": 0,
  "produced_files": ["delivery.md"],
  "known_risks": []
}
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

Every required Markdown artifact must also contain:

每个必需 Markdown artifact 也必须包含：

```markdown
artifact_result_code: 0
```

Business verdicts must use structured machine fields such as `tester_result.json.status`, `review_result.json.review_decision_code`, `peer_review_code`, and `final_delivery_code`. Do not copy those verdict codes into `return_code` or `artifact_result_code`.

业务判断必须使用结构化机器字段，例如 `tester_result.json.status`、`review_result.json.review_decision_code`、`peer_review_code` 和 `final_delivery_code`。不要把这些业务判断码复制到 `return_code` 或 `artifact_result_code`。

### Patch Gate / 补丁门禁

`PATCH_MERGE` produces the authoritative `merged_patch.diff`. Harness validates it before tester, reviewer, or communicator can trust it.

`PATCH_MERGE` 产出权威 `merged_patch.diff`。Harness 会先验证它，再交给 tester、reviewer 和 communicator。

The gate checks:

门禁检查：

- legal unified diff format / 合法 unified diff 格式
- allowed changed files / 修改范围
- `git apply --check`
- `git diff --check`
- abnormal size and mass deletion / 异常大小和大量删除
- sensitive paths such as `.git`, `.env`, keys, and tokens / 敏感路径

For new projects, Harness can materialize a `source/` directory from the final patch. For existing-project workflows, `system.source_repo` provides the read-only baseline input. Harness copies that source into each agent workspace repo and initializes a local `.git` baseline there, so agents can edit files and generate normal `git diff` patches from their isolated workspace.

新项目可以从最终 patch 物化 `source/` 目录。已有项目的 `bugfix` 和 `feature_change` 使用 `system.source_repo` 作为只读基线输入。Harness 会把 source 复制到每个 agent 的 workspace repo，并在副本里初始化本地 `.git` 基线，所以 agent 应该只改自己的隔离 workspace，并用 `git diff` 产出标准 patch。

For external benchmark or caller-driven runs, the caller-owned checkout should remain a baseline/input workspace. Harness should make changes in isolated workspace repos, produce a patch artifact, and let the caller apply that patch to its own clean checkout or evaluator environment.

由外部 benchmark 或调用方驱动运行时，调用方拥有的 checkout 应保持为基线/输入工作区。Harness 应在隔离 workspace repo 中修改代码，产出 patch artifact，再由调用方把 patch 应用到自己的干净 checkout 或评测环境。

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

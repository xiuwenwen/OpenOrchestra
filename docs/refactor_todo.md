# OpenOrchestra Refactor Roadmap

目标：把当前可运行的工程逐步重构成边界清晰、可验证、可长期维护的工程，同时不破坏现有 CLI/UI/API 行为。

## Completed Baseline Guardrails

- [x] 保留现有命令入口、UI 路由、artifact 文件名和合同码语义。
- [x] 每次重构后运行全量 `pytest`。
- [x] 为每次结构性重构补 characterization tests，先证明旧行为，再移动代码。
- [x] 未跟踪生成目录不进入提交，例如 `workPlaces/`。

## Completed Runtime Configuration Boundary

- [x] 保留前端配置每个 role 使用哪个 CLI 的能力。
- [x] 保留前端配置每个 role 数量的能力。
- [x] 把 UI 直接改 YAML 的逻辑收敛到 `RuntimeConfigService`。
- [x] Orchestrator 通过配置服务读取 backend、role count、timeout。
- [x] 任务运行中锁定运行配置，避免执行中漂移。
- [x] 增加显式持久化选项：`persist=true` 时由配置服务原子写入配置文件。
- [x] 为持久化配置增加结构化 YAML writer，禁止 UI 字符串替换配置文件。

## Completed Artifact Visibility And Contract Boundary

- [x] 新建 `harness/artifacts/visibility.py`，承载 role/phase/round 可见性规则和解释器。
- [x] `Orchestrator` 只调用 visibility policy，不直接解释 artifact 表。
- [x] 保持 `ARTIFACT_VISIBILITY_RULES` 的旧导入路径兼容，避免测试和外部调用断裂。
- [x] 建立 artifact visibility policy 独立矩阵测试：关键 role/phase/round 输入 artifact 必须 exact match。
- [x] 覆盖 tester 隔离、test judge 当前轮、fixing 最近完整测试轮、多 planner peer review 等组合。
- [x] 引入 `RolePhaseContract`，把 role/phase 的 required outputs 和 visibility rules 绑定为显式合同。
- [x] Validator 继续兼容旧字符串错误数组，同时保留结构化诊断对象。
- [x] markdown 合同码继续支持“文件内搜索”，但结构化输出优先。
- [x] 前端区分 OUTPUT_INVALID、业务测试失败、patch gate 失败、agent 执行失败。

## Completed Workflow Engine Boundary

- [x] 新建 `harness/workflow/engine.py`。
- [x] 迁移 new project flow：planning -> execution/test -> reviewer verdict -> delivery。
- [x] 迁移 bugfix flow。
- [x] 迁移 feature change flow。
- [x] 迁移 misc flow。
- [x] `Orchestrator.run_task()` 保持 facade，不改变调用方。
- [x] 现有 `harness/workflow/*.py` 空壳要么补成真实模块，要么删除。

## Completed Agent Run Boundary

- [x] 新建 `harness/agents/runner.py`。
- [x] 迁移 workspace 创建、adapter 调用、heartbeat、retry、timeout、artifact collection。
- [x] `run_role_phase()` 只负责兼容 facade，实际 phase 生命周期由 runner 承载。
- [x] timeout 语义统一：attempt timeout、phase timeout、cancel late result 分开记录。
- [x] OUTPUT_INVALID 产物是否登记要有显式策略，而不是混在 retry 逻辑里。

## Completed State Layer Hardening

- [x] Repository 禁止 Orchestrator 访问 `_lock` 和任务查询裸 SQL。
- [x] 增加语义方法：`set_task_workflow_type()`、`latest_task_id()`。
- [x] SQLite 增加索引：`task_id`、`phase_id`、`artifact_type`、`created_at`。
- [x] 增加唯一约束或事务策略，避免 artifact version 竞争。
- [x] 明确 phase/task/agent 状态机，禁止非法状态跳转。
- [x] 增加 append-only `events` 表，持久记录 progress event，支持失败、resume 和轮次行为审计。

## Completed UI Server Boundary

- [x] 文件读取安全策略拆出到 `HarnessFileReader`。
- [x] API handler 拆出到 `harness/ui/api.py`。
- [x] `HarnessStateView` 和 `UiEventStore` 拆出到 `harness/ui/state_view.py`。
- [x] UI 翻译逻辑拆出到 `harness/ui/translation.py`。
- [x] HTML/JS 模板拆出到 `harness/ui/html.py`。
- [x] UI API 增加稳定错误码。
- [x] UI API 增加基础请求 schema validation。
- [x] 配置 UI 显示当前配置来源：runtime、task override、persisted default。
- [x] 配置 UI 明确保存范围：仅当前运行时、未来任务默认值、或指定任务。
- [x] `harness/ui/html.py` 改为静态 HTML/CSS/JS 资源加载器，模板资源拆到 `harness/ui/static/`。

## Completed Orchestrator Decomposition

- [x] `harness/gates/test_gate.py` 承载 Harness-run build/test gate。
- [x] `harness/gates/patch_gate.py` 承载 patch validation/materialization gate。
- [x] `harness/materialization/service.py` 承载 source repo 选择、materialized repo 查找、成功标记和 repo metadata。
- [x] `harness/context/staging.py` 承载 input artifact staging、manifest 生成、tester target 嵌入和失败轮次上下文。
- [x] `Orchestrator` 保留旧 helper 名称作为兼容 facade，但不再直接实现上述业务细节。

## Completed Delivery And Cleanup

- [x] final delivery 发布逻辑从 Orchestrator 拆出。
- [x] 清理 `myHarnessSystem` 旧名称残留；仅保留 documented legacy alias 兼容。
- [x] 清理 pycache、历史生成目录、临时 workspace。
- [x] 架构文档同步到真实模块边界。

## Remaining Architecture Work

- [x] 把 `WorkflowEngine` 对 `Orchestrator._*` 私有 helper 的调用改为协议接口，例如 `PhaseRunner`、`GateRunner`、`DeliveryService`。
- [ ] 把 `ROLE_INSTRUCTIONS` 也纳入 `RolePhaseContract` 或相邻 role contract，避免 prompt 合同散落。
- [ ] 为 subprocess 安全加架构测试：禁止 `shell=True`，禁止字符串命令进入 `SubprocessRunner`。
- [ ] 给 backend adapter 增加健康状态、退避和 circuit breaker，减少 provider 故障时的无效重试。
- [ ] 给 artifact/workspace/deliver 增加可配置 retention policy，支持按 task、时间、失败保留策略清理。
- [ ] 如果未来转为多用户或多机器 worker，再考虑拆成真正服务；当前保持本地模块化架构。

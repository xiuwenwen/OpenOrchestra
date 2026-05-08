# OpenOrchestra Refactor Todo

目标：把当前可运行的工程逐步重构成边界清晰、可验证、可长期维护的工程，同时不破坏现有 CLI/UI/API 行为。

## 0. Baseline Guardrails

- [x] 保留现有命令入口、UI 路由、artifact 文件名和合同码语义。
- [x] 每次重构后运行全量 `pytest`。
- [ ] 为每次结构性重构补 characterization tests，先证明旧行为，再移动代码。
- [ ] 未跟踪生成目录不进入提交，例如 `workPlaces/`。

## 1. Runtime Configuration Boundary

- [x] 保留前端配置每个 role 使用哪个 CLI 的能力。
- [x] 保留前端配置每个 role 数量的能力。
- [x] 把 UI 直接改 YAML 的逻辑收敛到 `RuntimeConfigService`。
- [x] Orchestrator 通过配置服务读取 backend、role count、timeout。
- [x] 任务运行中锁定运行配置，避免执行中漂移。
- [ ] 增加显式持久化选项：`persist=true` 时由配置服务原子写入配置文件。
- [ ] 为持久化配置增加结构化 YAML writer，禁止 UI 字符串替换配置文件。

## 2. Artifact Visibility Boundary

- [x] 新建 `harness/artifacts/visibility.py`，承载 role/phase/round 可见性规则和解释器。
- [x] `Orchestrator` 只调用 visibility policy，不直接解释 artifact 表。
- [x] 保持 `ARTIFACT_VISIBILITY_RULES` 的旧导入路径兼容，避免测试和外部调用断裂。
- [ ] 建立 artifact universe 矩阵测试：每个 role/phase/round 的输入 artifact 必须 exact match。
- [ ] 覆盖多 planner、多 executor、多 tester、不同轮次、无效输出 fallback、final handoff 等组合。

## 3. Workflow Engine Boundary

- [x] 新建 `harness/workflow/engine.py`。
- [x] 迁移 new project flow：planning -> execution/test -> review -> final judgement -> delivery。
- [x] 迁移 bugfix flow。
- [x] 迁移 feature change flow。
- [x] 迁移 misc flow。
- [x] `Orchestrator.run_task()` 保持 facade，不改变调用方。
- [ ] 现有 `harness/workflow/*.py` 空壳要么补成真实模块，要么删除。

## 4. Agent Run Boundary

- [x] 新建 `harness/agents/runner.py`。
- [x] 迁移 workspace 创建、adapter 调用、heartbeat、retry、timeout、artifact collection。
- [x] `run_role_phase()` 只负责兼容 facade，实际 phase 生命周期由 runner 承载。
- [x] timeout 语义统一：attempt timeout、phase timeout、cancel late result 分开记录。
- [ ] OUTPUT_INVALID 产物是否登记要有显式策略，而不是混在 retry 逻辑里。

## 5. State Layer Hardening

- [x] Repository 禁止 Orchestrator 访问 `_lock` 和任务查询裸 SQL。
- [x] 增加语义方法：`set_task_workflow_type()`、`latest_task_id()`。
- [x] SQLite 增加索引：`task_id`、`phase_id`、`artifact_type`、`created_at`。
- [ ] 增加唯一约束或事务策略，避免 artifact version 竞争。
- [ ] 明确 phase/task/agent 状态机，禁止非法状态跳转。

## 6. UI Server Boundary

- [ ] 拆分 `HarnessStateView`、API handler、HTML/JS 字符串、file reader。
- [ ] UI API 增加 schema validation 和稳定错误码。
- [ ] 配置 UI 显示当前配置来源：runtime、task override、persisted default。
- [ ] 配置 UI 明确保存范围：仅当前运行时、未来任务默认值、或指定任务。

## 7. Artifact Contract Boundary

- [ ] 把 required outputs、合同码、visibility 可见性声明归并到 schema 层。
- [ ] Validator 返回结构化诊断对象，而不是字符串数组。
- [ ] markdown 合同码继续支持“文件内搜索”，但结构化输出优先。
- [ ] 前端区分 OUTPUT_INVALID、业务测试失败、patch gate 失败、agent 执行失败。

## 8. Delivery And Cleanup

- [ ] final delivery 发布逻辑从 Orchestrator 拆出。
- [ ] 清理 `myHarnessSystem` 旧名称残留。
- [ ] 清理 pycache、历史生成目录、临时 workspace。
- [ ] 架构文档同步到真实模块边界。

## Execution Order

1. Runtime configuration boundary.
2. Artifact visibility boundary.
3. Workflow engine boundary.
4. Agent run boundary.
5. State layer hardening.
6. UI server split.
7. Artifact contract consolidation.
8. Documentation and cleanup.

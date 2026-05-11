# OpenOrchestra Architecture Review Todo

日期：2026-05-11

审视角度：以 bounded context、模块化单体、可靠性、可观测性、状态一致性、交付边界为标准审视当前工程。OpenOrchestra 当前本质是本地 Agent 编排器，不应过早拆成网络微服务；合理目标是先做成边界清晰的模块化单体，未来如有多用户、多机器 worker、远程队列、团队协作需求，再按 bounded context 抽服务。

## 结论

当前工程方向是合理的：它没有重新实现 Agent 的 File/Shell/Edit/Test 工具，而是聚焦在任务编排、状态、artifact、patch gate、test gate、judge 和交付。近期重构已经把 workflow、agent runner、artifact visibility、gate、materialization、delivery、UI 等边界拆出来，工程可测试性也明显提高。

但它还不是优秀架构。主要问题是：核心边界仍靠 Orchestrator 兼容 facade 和大量字符串契约维持；prompt、artifact 合同、role/phase 可见性、状态机、round 语义、外部 CLI 调用、UI/CLI 入口仍存在交叉耦合；测试量很大但集中在少数巨型测试文件，长期维护成本高。

## 当前 Bounded Context

建议把当前模块化单体明确为以下上下文：

1. Task Intake Context
   - 负责 CLI、一次性命令、交互命令、workflow 分类、历史任务上下文。
   - 当前主要文件：`harness/main.py`、`harness/cli/*`、`harness/core/workflow_classifier.py`、`harness/core/misc_chat.py`。

2. Workflow Context
   - 负责 new_project、bugfix、feature_change、misc 的 phase sequencing。
   - 当前主要文件：`harness/workflow/engine.py`。

3. Agent Runtime Context
   - 负责 workspace、prompt、adapter 调用、retry、timeout、heartbeat、输出收集、输出校验。
   - 当前主要文件：`harness/agents/runner.py`、`harness/adapters/*`。

4. Artifact Contract Context
   - 负责 required outputs、delivery envelope、artifact result code、role/phase visibility、input staging。
   - 当前主要文件：`harness/artifacts/schemas.py`、`harness/artifacts/visibility.py`、`harness/artifacts/validator.py`、`harness/context/staging.py`。

5. Gate And Materialization Context
   - 负责 patch gate、test gate、materialized repo、repo source 选择。
   - 当前主要文件：`harness/gates/*`、`harness/patch/gate.py`、`harness/materialization/service.py`。

6. State And Event Context
   - 负责 tasks、phases、agent_runs、artifacts、judge_decisions、events。
   - 当前主要文件：`harness/state/*`。

7. Delivery Context
   - 负责 final_delivery、usage_guide、success_path、materialized source、dependency installer、交付 manifest。
   - 当前主要文件：`harness/workflow/delivery.py`、`harness/communication/communicator.py`。

8. UI Context
   - 负责本地 HTTP UI、SSE、状态视图、文件读取安全、前端静态资源。
   - 当前主要文件：`harness/ui/*`。

## 不合理之处

### A1. `main.py` 已收敛为入口装配模块

- 证据：`harness/main.py` 已降到约 130 行，只保留 argparse、配置加载、backend 选择、progress/UI/orchestrator 装配和最终 dispatch。delivery handoff 已抽到 `harness/delivery/handoff.py`，terminal dashboard 已抽到 `harness/ui/terminal_dashboard.py`，用户 env/config 映射已抽到 `harness/config/user_env.py`，CLI 命令注册和别名解析已抽到 `harness/cli/commands.py`，交互循环已抽到 `harness/cli/interactive.py`，workflow 分类和一次性执行已抽到 `harness/cli/runtime.py`，UI 启动已抽到 `harness/ui/launcher.py`。
- 风险：入口耦合风险已明显下降；后续风险转移到 `harness/cli/interactive.py` 内部方法仍偏多，需要在功能演进时继续拆 history/resume/cleanup 子服务。
- 目标：保持 `main.py` 作为 wire-up 文件，禁止重新塞入交互逻辑、dashboard 渲染、handoff 推断、env 映射或 command registry。

### A2. `Orchestrator` 已收敛为 facade，legacy helper 仅兼容保留

- 证据：`harness/app/bootstrap.py` 已接管服务构造和依赖注入，`AgentPhaseRunner` 和 `WorkflowEngine` 已改为调用公开 runtime API；`tests/test_workflow_engine_contract.py` 扫描生产代码，禁止继续通过 `o._*`、`orchestrator._*`、`runtime._*` 调用 Orchestrator 私有 helper。
- 风险：Orchestrator 内部仍保留部分 `_run_*`、`_latest_*`、`_stage_*` 兼容包装给历史测试和外部调用，后续应逐步删除未使用包装。
- 目标：保持 Orchestrator 作为应用服务 facade；新生产代码只调用公开 runtime/service API。

### A3. Prompt 合同、角色指令、输出合同散落

- 证据：
  - 已新增 `harness/contracts/role_contracts.py` 作为合同入口，但 prompt specialization 和 visibility 仍需继续向 contract registry 收敛。
  - planner/tester specializations 在 `harness/prompts/builder.py`。
  - output contract lines 在 `harness/artifacts/schemas.py`。
  - prompt template 文件也存在 `harness/prompts/templates/`。
- 风险：修改一个 role 的输出合同或任务分类提示词时，容易漏改另一个位置，导致 Agent prompt 与 validator 不一致。
- 目标：建立 `RoleContractRegistry`，每个 workflow_type + role + phase 绑定 required_outputs、visibility、output contract、role instruction、specialization、validator hint。

### A4. 状态机仍以字符串和宽松表驱动

- 证据：已新增 `harness/state/transitions.py`，task/phase/agent run 更新会校验 transition table；但 task 状态仍兼容历史 phase type，包括 `FINAL_JUDGEMENT`、`PLAN_JUDGEMENT`。
- 风险：legacy phase 仍会出现在 UI/历史数据中，后续要继续把 legacy phase 标为 deprecated 并从新流程入口移除。
- 目标：保留 transition table，继续收紧 task status 与 phase type 的关系，并为 legacy resume 提供显式兼容层。

### A5. Round ID 语义过载已开始结构化

- 证据：phase 表已新增 `loop_type`、`parent_round_id`、`iteration_id`；regression round 不再使用 `review_round_id * 1000 + test_round_id` 编码，而是沿用连续 round 并用结构字段表达 loop 归属。旧 `round_id` 仍保留用于路径、历史 artifact 和 UI 显示兼容。
- 风险：大部分可见性和历史查询仍以 `round_id` 为排序键，虽然 regression stride 已移除，但后续如果要展示多层 loop，需要进一步让 UI 和 explain 工具展示结构字段。
- 目标：保持 `round_id` 仅做兼容显示和路径字段，新的 loop 语义都写入结构字段。

### A6. Artifact 合同仍依赖 Markdown 字段搜索

- 证据：validator 支持在 Markdown 文件内搜索 `artifact_result_code`；delivery envelope 是 JSON，但业务报告仍是 Markdown + 字段。
- 风险：人类可读和机器可读混在同一个文件，模型容易把业务失败码误写到合同码；后续字段扩展会继续靠正则。
- 目标：每个交付文件旁边生成结构化 sidecar metadata，或统一交付 JSON envelope；Markdown 只负责人类说明，机器判定只读结构化文件。

### A7. Visibility table 已集中，已生成 matrix

- 证据：`harness/artifacts/schemas.py` 有 `ARTIFACT_VISIBILITY_RULES`，`harness/artifacts/visibility.py` 解释规则；`harness/contracts/visibility_matrix.py` 会从 `RolePhaseContract` 自动生成 `docs/generated_visibility_matrix.md`，CI 中用 `--check` 防止规则和文档漂移。
- 风险：matrix 能说明静态允许规则，但还不能解释某一次 task 中每个 artifact 被允许/拒绝的动态原因。
- 目标：继续增加 `orchestra visibility explain <task_id> <role> <phase> [round]` 或内部命令，输出每个 artifact 被允许/拒绝的规则原因。

### A8. Test gate 绕过统一 subprocess runner

- 证据：已新增 `harness/adapters/command_runner.py`，test gate 和 patch gate 已收敛到统一 runner；后续还需把 classifier、delivery review 等新增命令路径持续纳入架构测试。
- 风险：超时、日志、实时输出、命令安全、环境注入、未来 correlation id 行为不一致。
- 目标：建立 `CommandRunner` port；Agent、test gate、patch gate、classifier、delivery review 都走同一执行抽象。

### A9. 外部 Agent backend 已有基础健康状态和 circuit breaker

- 证据：`harness/adapters/health.py` 已新增 `BackendHealthMonitor`，`AgentPhaseRunner` 在每次 agent attempt 前检查 backend state，并在 timeout/auth/runtime failure 后记录 degraded/open；UI snapshot 和前端摘要会展示 open/degraded backend。
- 风险：当前熔断仍是进程内状态，尚未持久化到 state DB，也没有自动 fallback backend 策略。
- 目标：后续如要长驻服务化运行，再把 backend health 写入状态表，并增加可配置 fallback backend。

### A10. 输入 artifact budget 是全局数值，不是 role/phase 策略

- 证据：`artifact_input.max_files/max_file_bytes/max_total_bytes` 是全局配置；staging 里再按 artifact mode 做 path_only/truncate。
- 风险：tester、judge、reviewer、communicator 对上下文需求完全不同，用全局 budget 容易要么过多要么过少。
- 目标：将 budget 下沉到 `RolePhaseContract`，让每个 role/phase 有独立 max_files、max_bytes、large_artifact_mode、mandatory artifacts。

### A11. DeliveryPublisher 仍反向依赖 Orchestrator

- 证据：`harness/workflow/delivery.py` 已改为注入 config、repository、usage guide/materialized repo/source repo providers，不再持有 Orchestrator 或调用 `o._*` 私有方法。
- 风险：Delivery context 的职责已收窄，但 Orchestrator 仍保留若干 `_publish_*` 兼容 helper 给旧测试和调用点。
- 目标：继续缩小 Orchestrator 兼容 helper，最终让生产调用只通过公开 delivery publisher/service API。

### A12. Patch gate 分层仍偏混合

- 证据：`harness/gates/patch_gate.py` 是 service 层，但底层 `harness/patch/gate.py` 同时负责 diff 分析、git apply、materialized repo、命令执行。
- 风险：策略规则、git 命令执行、文件系统 materialization 耦合，难以替换或独立审计。
- 目标：拆成 `PatchAnalyzer`、`PatchPolicyEvaluator`、`GitApplyChecker`、`Materializer`、`PatchGateReportWriter`。

### A13. UI API 仍是手写 HTTP handler，缺少稳定 API schema

- 证据：`harness/ui/api.py` 使用 `BaseHTTPRequestHandler` 手写路由和 JSON schema。
- 风险：随着 UI 配置项增加，schema validation 和 error contract 容易散落；SSE 长连接、文件读取、配置更新缺少版本化 API。
- 目标：保留轻量 server 也可以，但需要明确 `/api/v1/*` schema、request/response dataclass 或 pydantic-lite validation。

### A14. 测试结构过于集中，已有 metrics guardrail

- 证据：`docs/generated_architecture_metrics.md` 记录当前 baseline：生产 Python 约 1.33 万行，最大生产文件是 `harness/core/orchestrator.py`，最大测试文件是 `tests/test_orchestrator_mock_flow.py`；`tests/test_architecture_metrics.py` 用宽阈值防止继续膨胀。
- 风险：新增行为时容易把测试继续塞进同一个文件，定位失败成本高，测试夹具重复。
- 目标：继续按 bounded context 拆分：`test_workflow_engine_*`、`test_agent_runner_*`、`test_input_staging_*`、`test_delivery_publisher_*`、`test_materialization_service_*`。

### A15. 文档和当前流程存在漂移

- 证据：`system_architecture_and_flow.md` 的状态图仍包含 `REVIEW_JUDGEMENT`、`FINAL_JUDGEMENT` 路径，但当前 workflow 已在 review loop 中直接读取 reviewer verdict，不再进入最终 judge。
- 风险：新维护者会按旧文档理解流程，继续往已废弃 phase 上加功能。
- 目标：文档由 workflow contract 或 Mermaid 生成脚本生成，至少在 CI 中检测关键 phase 是否和代码一致。

### A16. 生成目录清理已有 retention service

- 证据：`harness/retention/service.py` 已集中处理 task 清理，支持 dry-run、保护运行中任务、保留 final delivery/response，只删除 workspace/artifact 中间目录；`/clean` 已改为调用该服务。
- 风险：目前只覆盖按 task 清理，还没有按时间、大小、最近 N 次失败的自动保留策略。
- 目标：继续扩展为批量 retention profile 和 diagnostics bundle。

### A17. 可观测性已有基础 trace/span

- 证据：`ProgressEvent`、state DB events 和 UI event store 已带 `trace_id`、`span_id`、`parent_span_id`；`Orchestrator._emit` 会自动以 `task_id` 作为 trace_id 并生成默认 span。
- 风险：当前 span 还没有覆盖外部 command 子步骤和 artifact manifest，后续诊断包仍需要继续整合日志路径。
- 目标：继续让 gate command、artifact manifest、diagnostics bundle 复用同一 trace/span。

### A18. 配置层缺少统一 schema 和版本迁移

- 证据：`config/config.yaml`、env specs、UI runtime payload、task configuration JSON 分散定义；legacy env alias 也在 CLI 入口。
- 风险：新增配置项容易漏掉 env、UI、task override、README 四处之一。
- 目标：建立 `ConfigSchema`，统一 defaults、env mapping、UI-editable metadata、task override validation、doc generation。

### A19. 多 agent 并发模型已有基础资源隔离

- 证据：`BackendBulkheadScheduler` 已支持 `backend_concurrency`、`role_concurrency` 和 `scheduler.global_concurrency`，并包住实际 adapter 调用；等待时会响应 phase cancel event。
- 风险：当前 bulkhead 是进程内 semaphore，暂不适用于多进程或远程 worker；还没有 provider token budget。
- 目标：如果未来引入远程 worker 或多进程执行，再把 bulkhead 状态提升到共享调度服务。

### A20. 领域模型仍以 dict 穿透

- 证据：repository 已返回 `TaskRecord`、`PhaseRecord`、`AgentRunRecord`、`ArtifactRecord` 等兼容映射的 typed records；但 workflow、visibility、delivery 仍大量使用 `record["field"]` 访问。
- 风险：字段拼写、缺省值、类型转换仍有一部分散落在业务层，后续应逐步改为属性访问或专用 query。
- 目标：继续减少业务层字符串字段访问，让 repository/query service 提供更明确的领域查询。

## 分阶段 Todo

### Phase 0：冻结现有行为和文档真实度

- [x] T0.1 更新 `system_architecture_and_flow.md`
  - 范围：删除已废弃的 `FINAL_JUDGEMENT` 主流程，标明 `REVIEW_JUDGEMENT` 仅为 legacy/compat 或彻底移除。
  - 验收：文档里的 phase 顺序与 `WorkflowEngine` 一致；README 的 workflow 描述同步。
  - 测试：新增文档一致性测试，至少断言当前主流程 phase 名称不出现废弃路径。

- [x] T0.2 生成当前 role/phase visibility matrix
  - 范围：从 `RolePhaseContract` 自动生成 Markdown 表，列出 target role、phase、source role、artifact types、round policy。
  - 验收：生成文件进入 `docs/generated_visibility_matrix.md`；CI 检查生成内容未漂移。
  - 测试：新增 golden test 或 snapshot hash test。

- [x] T0.3 增加 architecture metrics check
  - 范围：记录核心文件 LOC、最大函数长度、测试文件 LOC。
  - 验收：CI 只警告不失败；文档中列出当前 baseline。
  - 测试：新增 `tests/test_architecture_metrics.py`，先以宽阈值保护不继续恶化。

### Phase 1：合同集中化

- [x] T1.1 建立 `harness/contracts/role_contracts.py`
  - 范围：迁移 role instruction、required outputs、visibility rules、output contract lines、prompt specialization selector。
  - 验收：`core/orchestrator.py` 不再定义 `ROLE_INSTRUCTIONS`；`prompts/builder.py` 从 contract registry 读取 specializations。
  - 测试：迁移并扩展 `tests/test_artifact_schemas.py`，覆盖每个 workflow_type + role + phase。

- [x] T1.2 增加 role/phase artifact budget
  - 范围：在 contract 中定义 `input_budget`，替代 staging 中的全局唯一 budget。
  - 验收：tester、judge、reviewer、communicator 的 budget 可分别配置；默认行为与当前测试一致。
  - 测试：为 tester/judge/reviewer/communicator 各加一个 budget exact-match 测试。

- [ ] T1.3 引入结构化 artifact metadata
  - 范围：保留 Markdown 兼容，但每个 artifact collection 同时写入/登记 machine metadata。
  - 验收：validator 优先读 metadata；Markdown 字段搜索作为 legacy fallback。
  - 测试：同一 artifact 在 metadata 正确、Markdown 缺字段时仍通过；metadata 错误时拒绝。

### Phase 2：状态和 round 模型硬化

- [x] T2.1 引入 typed records
  - 范围：`TaskRecord`、`PhaseRecord`、`AgentRunRecord`、`ArtifactRecord`。
  - 验收：workflow、visibility、delivery 不再直接依赖裸 dict；UI adapter 可在边界转换成 JSON。
  - 测试：repository contract tests 覆盖类型转换和缺省值。

- [x] T2.2 建立状态机 transition table
  - 范围：task/phase/agent run 的合法状态转换。
  - 验收：非法转换抛出明确异常；legacy resume 恢复路径有显式例外。
  - 测试：覆盖成功、失败、checkpoint recovery、timeout、OUTPUT_INVALID。

- [x] T2.3 解开 round_id 编码
  - 范围：新增 `loop_type`、`parent_round_id`、`iteration_id` 字段；保留 `round_id` 显示兼容。
  - 验收：regression round 不再依赖 `1000` stride；phase 记录可用结构字段解释 loop 归属。
  - 测试：迁移现有 regression round tests，保留旧 artifact 兼容测试。

### Phase 3：执行与可靠性边界

- [x] T3.1 统一 command execution port
  - 范围：`SubprocessRunner` 扩展为 `CommandRunner`，覆盖 adapter、test gate、patch gate、classifier、delivery review。
  - 验收：所有 subprocess 调用都走统一 runner；禁止 `shell=True`；命令列表形式强制校验。
  - 测试：新增架构测试，扫描 `subprocess.run/Popen` 只允许在 runner 内部出现。

- [x] T3.2 backend health 和 circuit breaker
  - 范围：为每个 backend 维护连续失败、失败类型、cooldown、熔断状态。
  - 验收：provider 系统性失败时不继续无效重试；UI 显示 backend degraded/open。
  - 测试：模拟连续 REQUEST_SIZE、timeout、auth failure、non-retryable failure。

- [x] T3.3 Scheduler bulkhead
  - 范围：按 backend/role 全局限制并发，支持排队和取消。
  - 验收：配置 `backend_concurrency.claude=1` 时同一时刻只有一个 Claude run。
  - 测试：并发 phase 测试验证排队、超时、取消传播。

### Phase 4：Orchestrator 和 Delivery 解耦

- [x] T4.1 拆 application bootstrap
  - 范围：新增 `harness/app/bootstrap.py`，负责服务构造和依赖注入。
  - 验收：`Orchestrator.__init__` 不再手动 new 所有服务；测试可以单独构造 bounded context。
  - 测试：bootstrap wiring smoke test。

- [x] T4.2 DeliveryPublisher 改为 ports 注入
  - 范围：移除 `orchestrator: Any`，注入 repository、communicator、materialized repo、source repo、artifact writer。
  - 验收：`DeliveryPublisher` 不调用任何 `o._*` 方法。
  - 测试：独立 delivery publisher tests，不依赖完整 Orchestrator。

- [x] T4.3 移除 Orchestrator legacy helper 依赖
  - 范围：统计 `_run_*`、`_latest_*`、`_stage_*` 兼容方法调用点，迁移到 service port。
  - 验收：生产代码不再调用 Orchestrator 私有 helper；测试只通过公开 service API 或专用 fixture。
  - 测试：扩展 `test_workflow_engine_contract.py` 的架构扫描。

### Phase 5：CLI/UI 拆分

- [x] T5.1 拆 `InteractiveCLI`
  - 范围：命令解析、history/resume/continue、workflow classification、task execution 分文件。
  - 验收：`main.py` 少于 300 行；原交互命令和一次性命令不变。
  - 测试：覆盖 command parser、resume context、one-shot command 和入口装配边界。

- [x] T5.2 拆 Dashboard
  - 范围：`DashboardProgressReporter` 移入 `harness/ui/terminal_dashboard.py`。
  - 验收：dashboard 渲染、event line、状态更新可独立单测。
  - 测试：宽字符、截断、向上滚动、非 TTY fallback。

- [x] T5.3 Handoff 推断独立化
  - 范围：delivery run command、dependency install、source path 判断移入 `harness/delivery/handoff.py`。
  - 验收：CLI 只调用 handoff service 并打印结果。
  - 测试：Python/Node/partial materialized source/usage guide 命令提取。

### Phase 6：可观测性和清理策略

- [x] T6.1 correlation id/span id
  - 范围：task event、phase event、agent run、command run、artifact manifest 统一记录 trace/span。
  - 验收：给定 task_id 可以完整追踪 agent prompt、stdout/stderr、gate command、artifact、delivery。
  - 测试：端到端 mock flow 中断言每类事件都有 trace 字段。

- [x] T6.2 retention policy
  - 范围：按 `workspaces/`、`artifacts/`、`deliver/`、`state/events` 定义保留规则。
  - 验收：支持 dry-run、按 task 清理、保留成功交付、保留失败最近 N 次。
  - 测试：清理命令不会删除 current active task；不会删除 final delivery。

- [ ] T6.3 operational diagnostics bundle
  - 范围：失败任务一键导出 prompt、manifest、stdout/stderr、gate reports、decision、event timeline。
  - 验收：`orchestra diagnose <task_id>` 生成 zip 或目录，隐私字段可脱敏。
  - 测试：mock failure 生成完整 bundle。

## 不建议现在做的事

- 不建议立即拆成真正微服务。当前没有多团队、独立部署、远程 worker 池、不同扩缩容需求；拆服务会制造 distributed monolith。
- 不建议把 Markdown 报告一次性全部改成 JSON。更合理路径是先添加结构化 metadata，保持用户可读 Markdown 和历史兼容。
- 不建议一次性删除 Orchestrator 的所有兼容 helper。先通过 ports 和 contract tests 缩小依赖面，再分批移除。
- 不建议让前端直接配置所有低层策略。前端应配置业务上可理解的 backend/count/timeout/budget profile，底层策略保留在配置文件或高级模式。

## 按优先级排序的执行清单

### P0：立即做，阻断继续返工的问题

这些任务会继续制造错误理解、错误实现或安全风险，应优先完成。

1. [x] T0.1 更新 `system_architecture_and_flow.md`
   - 原因：当前文档仍描述已废弃或弱化的 judgement/final judgement 路径，会误导后续开发。
   - 交付：更新架构图、workflow 描述、README 对应段落。
   - 验收：文档中的主流程与 `WorkflowEngine` 当前行为一致。

2. [x] T1.1 建立 `harness/contracts/role_contracts.py`
   - 原因：prompt、required outputs、visibility、输出合同散落，是 role 输入和合同码问题反复出现的根因。
   - 交付：`RoleContractRegistry`，统一 workflow_type + role + phase 的合同入口。
   - 验收：`core/orchestrator.py` 不再定义 `ROLE_INSTRUCTIONS`；prompt builder 和 validator 从合同入口读取。

3. [x] T3.1 统一 command execution port
   - 原因：Agent、test gate、patch gate、classifier 等外部命令路径不完全统一，安全、超时、日志、取消语义会漂移。
   - 交付：`CommandRunner` port，所有外部命令调用统一走列表参数形式。
   - 验收：生产代码中 `subprocess.run/Popen` 只允许出现在 runner 实现内；无 `shell=True`。

### P1：高优先级，建立长期可维护骨架

这些任务不一定马上导致事故，但会决定后续重构是否越做越稳。

4. [x] T1.2 增加 role/phase artifact budget
   - 原因：tester、judge、reviewer、communicator 需要的上下文不同，全局 budget 不能表达差异。
   - 交付：每个 role/phase 独立配置 max_files、max_bytes、large_artifact_mode、mandatory artifacts。
   - 验收：tester/judge/reviewer/communicator 的输入 manifest 有 exact-match 测试。

5. [x] T2.1 引入 typed records
   - 原因：裸 dict 穿透 workflow、visibility、delivery、UI，字段错误和类型漂移难以控制。
   - 交付：`TaskRecord`、`PhaseRecord`、`AgentRunRecord`、`ArtifactRecord`。
   - 验收：repository 边界负责 dict/dataclass 转换；业务层不直接读取裸 SQLite row。

6. [x] T2.2 建立状态机 transition table
   - 原因：当前状态主要是字符串集合检查，不能表达合法跳转。
   - 交付：task/phase/agent run transition table。
   - 验收：非法状态转换抛明确异常；checkpoint recovery、timeout、OUTPUT_INVALID 有显式路径。

7. [x] T4.2 DeliveryPublisher 改为 ports 注入
   - 原因：delivery 仍反向依赖 Orchestrator 私有 helper，边界没有真正独立。
   - 交付：注入 repository、communicator、materialized repo、source repo、artifact writer ports。
   - 验收：`DeliveryPublisher` 不调用任何 `o._*` 方法，可独立单测。

8. [x] T5.1 拆 `InteractiveCLI`
   - 原因：`main.py` 是最大耦合点，CLI、dashboard、handoff、env、UI 启动混在一起。
   - 交付：命令解析、resume context、workflow execution、one-shot command 分模块。
   - 验收：`main.py` 少于 300 行；现有交互命令和一次性命令行为不变。

### P2：中高优先级，解决规模化运行和长期成本

这些任务主要减少长任务、provider 故障、并发和磁盘增长带来的运行风险。

9. [x] T2.3 解开 round_id 编码
   - 原因：普通 round、review round、regression round 共用一个整数，会污染 visibility、UI 和 resume 逻辑。
   - 交付：新增 `loop_type`、`parent_round_id`、`iteration_id` 或等价模型。
   - 验收：regression round 不再依赖 `1000` stride；旧 artifact 仍兼容读取。

10. [x] T3.2 backend health 和 circuit breaker
    - 原因：provider 系统性失败时，只靠单 run retry 会继续烧 token 和时间。
    - 交付：backend 健康状态、连续失败计数、cooldown、熔断、降级策略。
    - 验收：模拟 timeout/auth failure/context limit 时，backend 能进入 degraded/open 状态。

11. [x] T3.3 Scheduler bulkhead
    - 原因：多 agent 并发缺少 backend/role 级资源隔离。
    - 交付：按 backend、role、全局并发限制排队执行。
    - 验收：配置 `backend_concurrency.claude=1` 时同一时刻只有一个 Claude run。

12. [x] T6.1 correlation id/span id
    - 原因：任务排障仍要人工串 prompt、manifest、stdout/stderr、gate report、decision。
    - 交付：task/phase/run/command/artifact 全链路 trace/span。
    - 验收：一个 task_id 能追踪所有关键事件和文件。

13. [x] T6.2 retention policy
    - 原因：workspace/artifact/deliver/state 会持续增长，用户已经多次关注空间浪费。
    - 交付：dry-run、按 task/时间/大小/状态清理、保留成功交付。
    - 验收：不会删除 active task；不会删除 final delivery。

### P3：优化项，提高可解释性和工程体验

这些任务价值明确，但可以在 P0-P2 稳定后做。

14. [x] T0.2 生成当前 role/phase visibility matrix
    - 原因：role 为什么看到某个 artifact 仍需要读代码。
    - 交付：`docs/generated_visibility_matrix.md`。
    - 验收：CI 检查生成内容未漂移。

15. [x] T0.3 增加 architecture metrics check
    - 原因：大文件和大测试文件已经出现，需要防止继续恶化。
    - 交付：核心文件 LOC、最大函数长度、测试文件 LOC baseline。
    - 验收：先警告不失败，后续逐步收紧。

16. [x] T4.1 拆 application bootstrap
    - 原因：服务构造仍集中在 Orchestrator。
    - 交付：`harness/app/bootstrap.py`。
    - 验收：Orchestrator 不再手动 new 所有服务。

17. [x] T4.3 移除 Orchestrator legacy helper 依赖
    - 原因：兼容 helper 会诱导新代码继续绕过 service boundary。
    - 交付：迁移 `_run_*`、`_latest_*`、`_stage_*` 调用到公开 service API。
    - 验收：生产代码不再调用 Orchestrator 私有 helper。

18. [x] T5.2 拆 Dashboard
    - 原因：dashboard 与 CLI 主入口耦合，终端渲染问题会污染交互逻辑。
    - 交付：`harness/ui/terminal_dashboard.py`。
    - 验收：宽字符、截断、向上滚动、非 TTY fallback 独立测试。

19. [x] T5.3 Handoff 推断独立化
    - 原因：交付路径、run command、dependency install 推断不应留在 CLI。
    - 交付：`harness/delivery/handoff.py`。
    - 验收：Python/Node/partial materialized source/usage guide 命令提取独立测试。

20. [ ] T6.3 operational diagnostics bundle
    - 原因：失败排障需要一键收集证据。
    - 交付：`orchestra diagnose <task_id>`。
    - 验收：mock failure 能导出 prompt、manifest、stdout/stderr、gate reports、decision、event timeline。

## 分阶段 Todo 说明

下面的分阶段 Todo 保留原来的工程推进视角。实际执行顺序以上面的 P0/P1/P2/P3 为准。

## 验收标准

- 所有主流程仍保持 CLI/UI/API 行为兼容。
- `python3 -m pytest -q` 全绿。
- 生产代码中没有新的 Orchestrator 私有 helper 依赖。
- 每个 role/phase 的 required outputs、visibility、prompt instruction、input budget 都能从一个合同入口查到。
- tester、judge、reviewer、communicator 的输入 manifest 可以用 explain 工具解释每个 artifact 的来源和规则。
- 任何外部命令调用都可追踪、可超时、可取消、无 `shell=True`。
- 失败任务可以从 event timeline、logs、artifact manifest 恢复完整诊断链路。

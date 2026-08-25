# RepoTerm Python 包结构重构执行契约

> 状态：待执行
> 任务类型：行为保持型结构重构
> 适用分支：`refactor/v2`
> 核心原则：只改变代码的位置、导入路径和组合边界，不改变 Agent 行为。

## 1. 任务目标

当前 `repoterm/` 根目录平铺了 Runtime、模型适配、上下文、会话、安全、可观测性、控制器、评测和 UI 等大量模块。模块职责虽然存在，但目录结构没有表达这些边界，导致：

- 阅读者无法从目录判断一次 Agent Turn 的主链路；
- Runtime、Provider、UI 和持久化模块相互直接导入；
- `agent_loop.py`、`session.py`、`cli_commands.py` 等大文件难以继续安全演进；
- 评测与产品运行代码混在同一个 Python 包中；
- 修改一个模块时难以判断影响范围和测试范围。

本任务需要在不减少 Coding Agent 能力、不改变外部行为的前提下，将源码按稳定职责重新分组，使目录能够直接表达以下能力边界：

```text
应用入口 → Agent Runtime → Model / Context / Tool
                              ├→ Safety
                              ├→ Memory
                              ├→ Session
                              └→ Observability
```

## 2. 本任务不做什么

本轮禁止同时进行以下修改：

- 不删除控制论、TUI、MCP、Skills、Memory、Session 或 AgentOps 功能；
- 不调整 `explore → execute → verify` 的阶段逻辑；
- 不修改 Prompt 文本、阈值、最大步数、重试次数和默认配置；
- 不修改 Tool 名称、JSON Schema、ToolResult 或工具具体行为；
- 不修改权限规则、Diff 审批、Checkpoint、Session、Delta、Trace 的数据格式；
- 不修改 Memory SQLite Schema、生命周期和注入策略；
- 不拆写 `agent_loop.py`、`session.py` 等大文件内部逻辑；
- 不引入 LangChain、依赖注入框架、事件总线框架或新的第三方依赖；
- 不通过 `try/except ImportError`、动态导入、重复实现或测试 skip 掩盖迁移错误；
- 不修改断言以迁就重构结果，不降低任何测试门槛；
- 不提交真实模型凭据，不推送远端，不改写 Git 历史。

大文件拆分是下一阶段任务。本阶段只能移动文件、增加必要的 `__init__.py`、修改 Import 和更新命令行入口。

## 3. 执行前必须冻结的基线

执行 Agent 必须先读取仓库根目录 `AGENTS.md`，然后完成以下只读检查：

```powershell
git status --short
git branch --show-current
git rev-parse --short HEAD
conda run -n agent-env python -m pytest -q
```

必须把以下信息记录到最终报告：

- 基线 Commit；
- 工作区是否干净；
- Python、Pytest 版本；
- 全量测试通过数、跳过数、失败数和耗时；
- `repoterm/` 下 Python 文件数和代码行数；
- `repoterm/` 根目录直接包含的 Python 模块数。

如果工作区存在非本任务修改，不得覆盖、还原或删除。无法安全绕开时停止并报告。

## 4. 目标目录

第一阶段采用以下结构。目录名称表达稳定工程职责，不按文件大小拆分：

```text
repoterm/
├── __init__.py
├── config.py                     # 跨入口共享配置；第一阶段保留稳定路径
├── app/                         # 产品组合入口
│   ├── interactive.py           # 原 main.py
│   ├── headless.py              # 原 headless.py
│   ├── readiness.py
│   ├── structure_check.py
│   ├── engineering_structure.py
│   └── install.py
├── contracts/                   # 跨模块稳定数据契约
│   ├── types.py
│   └── state.py
├── runtime/                     # 单个 Agent Turn 及任务执行策略
│   ├── loop.py                  # 原 agent_loop.py，第一阶段保持内部逻辑不变
│   ├── turn_kernel.py
│   ├── runtime_profiles.py
│   ├── auto_mode.py
│   ├── hooks.py
│   ├── reflection.py
│   ├── planning/
│   └── control/
├── providers/                   # 模型供应商与 Model Adapter
│   ├── registry.py
│   ├── anthropic.py
│   ├── openai.py
│   ├── mock.py
│   ├── retry.py
│   ├── fallback.py
│   └── switching.py
├── context/                     # Prompt 上下文与 Token 治理
│   ├── manager.py
│   ├── compactor.py
│   ├── micro_compact.py
│   ├── layered.py
│   ├── working_memory.py
│   └── circuit_breaker.py
├── tools/                       # Tool Runtime 与内置工具
│   ├── registry.py              # 原 tooling.py
│   └── <现有内置工具文件>
├── safety/                      # 工作区和副作用安全
│   ├── permissions.py
│   ├── file_review.py
│   ├── evidence.py
│   └── workspace.py
├── session/                     # 会话、Checkpoint、Delta、Replay
│   ├── __init__.py
│   └── service.py               # 原 session.py，第一阶段保持整体移动
├── memory/                      # 已完成的轻量记忆系统，本轮不改内部结构
├── observability/               # 日志、指标和决策记录
│   ├── logging.py
│   ├── metrics.py
│   ├── cost.py
│   └── decision_audit.py
├── integrations/                # 外部扩展协议
│   ├── mcp.py
│   └── skills.py
└── ui/                          # 交互界面
    ├── commands.py
    ├── tty.py
    ├── history.py
    ├── shortcuts.py
    ├── surfaces.py
    ├── manage.py
    └── tui/                     # 原 repoterm/tui/
```

以下评测实现必须离开产品运行包，但不能删除：

```text
repoterm/llm_e2e_eval.py          → benchmarks/evaluation/llm_e2e.py
repoterm/runtime_profile_eval.py  → benchmarks/evaluation/runtime_profile.py
repoterm/fallback_simulation.py   → repoterm/providers/fallback_simulation.py
```

Fallback Simulation 被产品 Readiness 入口直接使用，属于 Provider Readiness 支撑能力，不属于纯评测实现。

原有 `benchmarks/llm_e2e_eval.py` 等命令入口可以保留为薄包装器，但业务实现只能有一份。

## 5. 文件迁移映射

### 5.0 应用入口与公共契约

```text
main.py                   → app/interactive.py
headless.py               → app/headless.py
readiness.py              → app/readiness.py
structure_check.py        → app/structure_check.py
engineering_structure.py  → app/engineering_structure.py
install.py                → app/install.py

types.py                  → contracts/types.py
state.py                  → contracts/state.py
config.py                 → config.py（第一阶段保留稳定路径）
```

### 5.1 Runtime 主链路

```text
agent_loop.py              → runtime/loop.py
turn_kernel.py             → runtime/turn_kernel.py
runtime_profiles.py        → runtime/runtime_profiles.py
auto_mode.py               → runtime/auto_mode.py
hooks.py                   → runtime/hooks.py
agent_reflection.py        → runtime/reflection.py
```

### 5.2 Runtime Planning

```text
agent_intelligence.py      → runtime/planning/intelligence.py
agent_router.py            → runtime/planning/router.py
smart_router.py            → runtime/planning/smart_router.py
domain_classifier.py       → runtime/planning/domain_classifier.py
intent_parser.py           → runtime/planning/intent_parser.py
task_graph.py              → runtime/planning/task_graph.py
task_object.py             → runtime/planning/task_object.py
task_tracker.py            → runtime/planning/task_tracker.py
capability_registry.py     → runtime/planning/capability_registry.py
prompt.py                  → runtime/planning/prompt.py
prompt_pipeline.py         → runtime/planning/prompt_pipeline.py
```

### 5.3 Runtime Control

控制论相关能力本轮只集中位置，不删除、不重写：

```text
adaptive_pid_tuner.py      → runtime/control/adaptive_pid_tuner.py
context_cybernetics.py     → runtime/control/context_cybernetics.py
cost_control.py            → runtime/control/cost_control.py
cybernetic_ablation.py     → runtime/control/cybernetic_ablation.py
cybernetic_orchestrator.py → runtime/control/cybernetic_orchestrator.py
cybernetic_supervisor.py   → runtime/control/cybernetic_supervisor.py
decoupling_controller.py   → runtime/control/decoupling_controller.py
feedback_controller.py     → runtime/control/feedback_controller.py
feedforward_controller.py  → runtime/control/feedforward_controller.py
pipeline_engine.py         → runtime/control/pipeline_engine.py
predictive_controller.py   → runtime/control/predictive_controller.py
progress_controller.py     → runtime/control/progress_controller.py
self_healing_engine.py     → runtime/control/self_healing_engine.py
stability_monitor.py       → runtime/control/stability_monitor.py
state_observer.py          → runtime/control/state_observer.py
verification_controller.py → runtime/control/verification_controller.py
```

### 5.4 Provider

```text
model_registry.py          → providers/registry.py
anthropic_adapter.py       → providers/anthropic.py
openai_adapter.py          → providers/openai.py
mock_model.py              → providers/mock.py
api_retry.py               → providers/retry.py
model_switcher.py          → providers/switching.py
readiness_support.py       → providers/readiness_support.py
```

Provider Readiness 的产品命令入口位于 `app/readiness.py`，内部支持逻辑位于 `providers/`。

### 5.5 Context

```text
context_manager.py         → context/manager.py
context_compactor.py       → context/compactor.py
micro_compact.py           → context/micro_compact.py
layered_context.py         → context/layered.py
working_memory.py          → context/working_memory.py
circuit_breaker.py         → context/circuit_breaker.py
```

### 5.6 Safety、Session、Observability、Integration 和 UI

```text
permissions.py             → safety/permissions.py
file_review.py             → safety/file_review.py
evidence_safety.py         → safety/evidence.py
workspace.py               → safety/workspace.py

session.py                 → session/service.py
user_profile.py            → memory/legacy_user_profile.py

logging_config.py          → observability/logging.py
agent_metrics.py           → observability/metrics.py
cost_tracker.py            → observability/cost.py
decision_audit.py          → observability/decision_audit.py

mcp.py                     → integrations/mcp.py
skills.py                  → integrations/skills.py

cli_commands.py            → ui/commands.py
tty_app.py                 → ui/tty.py
history.py                 → ui/history.py
local_tool_shortcuts.py    → ui/shortcuts.py
product_surfaces.py        → ui/surfaces.py
manage_cli.py              → ui/manage.py
tui/                       → ui/tui/
```

`background_tasks.py` 与具体工具生命周期强相关，迁移至 `tools/background.py`。根目录的 `cron.example.json` 不是 Python 运行模块，本阶段保持位置不变并在最终报告中记录；不要为了目录整齐擅自改变用户可见示例路径。

## 6. 依赖方向约束

完成迁移后应遵守：

```text
app / ui
    ↓
runtime
    ├── providers
    ├── context
    ├── tools → safety
    ├── memory
    ├── session
    └── observability

integrations → tools/contracts
benchmarks → repoterm public modules
```

禁止新增以下反向依赖：

- `providers` 导入 `runtime.loop`；
- `session` 导入 `ui`；
- `memory` 导入 `ui` 或自行驱动 Prompt；
- `safety` 导入 `runtime.loop`；
- `observability` 改变业务状态；
- `benchmarks` 被 `repoterm` 生产代码导入；
- 内置 Tool 直接修改 Turn 状态。

当前 `tools/task.py` 对 Agent Runtime 的调用属于已存在的委派能力。第一阶段只更新其 Import，不顺手重构该依赖；在最终报告中记录为后续边界债务。

## 7. 分阶段执行动作

每个阶段必须独立完成、独立验证。上一个阶段未通过时，不得开始下一个阶段。

### 阶段 A：建立目录和稳定公共出口

动作：

1. 创建目标 Package 和 `__init__.py`；
2. 明确每个 Package 对外允许导出的符号；
3. 暂时不移动核心文件；
4. 为后续测试准备新的 Import 路径；
5. 不在 `__init__.py` 中导出大量内部实现，避免循环导入。

验证：

```powershell
conda run -n agent-env python -m compileall -q repoterm
conda run -n agent-env python -m pytest tests/contracts tests/memory -q
```

### 阶段 B：迁移低耦合模块

按以下顺序逐组迁移：

1. `observability/`；
2. `providers/`；
3. `safety/`；
4. `context/`；
5. `integrations/`。

每组动作：

1. 使用文件移动保留 Git rename 识别；
2. 更新生产代码 Import；
3. 更新对应测试 Import；
4. 用 `rg` 确认旧 Import 已清零；
5. 运行该组单测和 `tests/contracts`；
6. 通过后再迁移下一组。

不得同时对类名、函数名、参数或内部实现做格式化重写。

### 阶段 C：迁移 Tool、Session 和 Memory 兼容文件

动作：

1. 将 `tooling.py` 整体迁移为 `tools/registry.py`；
2. 更新所有内置 Tool、MCP、测试和 Runtime Import；
3. 将 `session.py` 整体迁移为 `session/service.py`；
4. 在 `session/__init__.py` 仅导出原有公共 Session API；
5. 将 `user_profile.py` 迁入 `memory/legacy_user_profile.py`，保持只读兼容行为；
6. 不修改 Session 和 Memory 持久化格式。

验证：

```powershell
conda run -n agent-env python -m pytest tests/test_tools.py tests/test_permissions.py tests/test_session.py tests/memory tests/contracts -q
```

### 阶段 D：迁移 Runtime、Planning 和 Control

动作：

1. 先迁移 Planning；
2. 再整体迁移 Control；
3. 再迁移 `turn_kernel.py` 和 `runtime_profiles.py`；
4. 最后迁移 `agent_loop.py`；
5. 保持 `run_agent_turn` 签名、返回值、事件顺序和默认 `enable_work_chain` 行为不变；
6. 不合并重复控制器，不修改阶段策略。

验证：

```powershell
conda run -n agent-env python -m pytest tests/test_turn_kernel.py tests/test_agent_loop.py tests/test_agent_flow.py tests/test_agentops_scenarios.py tests/contracts -q
```

### 阶段 E：迁移 UI 和应用入口

动作：

1. 迁移 `tui/`、TTY、命令和历史模块；
2. 迁移 Interactive、Headless、Readiness 和 Structure Check 入口；
3. 更新 `pyproject.toml`：

```toml
[project.scripts]
repoterm = "repoterm.app.interactive:main"
repoterm-headless = "repoterm.app.headless:main"
repoterm-readiness = "repoterm.app.readiness:main"
repoterm-structure-check = "repoterm.app.structure_check:main"
```

4. 保持所有命令行参数、退出码和帮助文本不变；
5. 更新测试的 Import；
6. 不改变界面视觉和输入行为。

验证：

```powershell
conda run -n agent-env python -m pytest tests/test_main.py tests/test_headless.py tests/test_cli_commands.py tests/test_tty_app.py tests/test_tui.py -q
conda run -n agent-env repoterm --help
conda run -n agent-env repoterm-headless --help
conda run -n agent-env repoterm-readiness --help
conda run -n agent-env repoterm-structure-check --help
```

### 阶段 F：迁移 Evaluation 并删除临时兼容入口

动作：

1. 将评测实现移动到 `benchmarks/evaluation/`；
2. 保留现有用户命令包装器，避免 README 命令失效；
3. 全仓搜索旧模块路径；
4. 删除已经没有消费者的根目录兼容转发文件；
5. 根目录不得残留只做 `import *` 的永久 shim；
6. 更新 README、Architecture和相关工程文档中的源码路径；
7. 如果 `AGENTS.md` 中存在与新 Python 包路径直接冲突的旧路径说明，只更新与本次迁移直接相关的路径，不扩写其他规则。

## 8. 全量验收

### 8.1 静态与导入验收

```powershell
conda run -n agent-env python -m compileall -q repoterm benchmarks tests
rg -n "repoterm\.(agent_loop|turn_kernel|tooling|permissions|session|model_registry|anthropic_adapter|openai_adapter|context_manager|context_compactor|micro_compact|tty_app|cli_commands)" repoterm tests benchmarks
```

第二条命令最终应无旧 Import 命中；文档中的历史说明可以单独审查，不能机械删除。

### 8.2 测试验收

```powershell
conda run -n agent-env python -m pytest -q
conda run -n agent-env python benchmarks/runtime_regression_eval.py --rounds 3
```

硬性门槛：

- 全量测试不得少于执行前基线，不得新增失败；
- 20个确定性Runtime场景三轮保持 `60/60`；
- 异常恢复相关场景保持通过；
- Memory、Session、Permission、Tool和Turn契约全部通过；
- 不允许新增 skip、xfail 或放宽断言。

真实模型测试需要用户确认成本和凭据后再运行：

```powershell
conda run -n agent-env python benchmarks/llm_e2e_eval.py --all --runs 1 --confirm-live
```

### 8.3 行为与性能验收

重构前后各执行至少5次以下指标，比较中位数：

- `import repoterm` 和核心入口 Import 时间；
- `repoterm --help` 启动时间；
- 20场景Runtime回归总耗时；
- 相同ScenarioModel任务的步骤数、工具调用数和停止原因。

接受标准：

- 确定性任务行为必须完全一致；
- 启动和回归耗时不得稳定恶化超过10%；
- 不得新增循环导入或模块级副作用；
- 纯结构迁移不应改变Token消耗、模型调用次数和工具调用顺序。

### 8.4 结构验收

- `repoterm/` 根目录只保留 `__init__.py` 和确有必要的包级文件；
- 生产能力能够从目录直接定位；
- 不存在同一实现的新旧两份副本；
- 不存在永久兼容 shim 堆积；
- Git 能将绝大多数变更识别为 rename，而不是整文件删除重写；
- `git diff --check` 无错误；
- `git status --short` 中不出现运行时缓存、报告、数据库或密钥文件。

## 9. 停止条件

遇到以下情况必须停止当前阶段并报告，不得自行扩大修改范围：

- 需要修改业务逻辑才能解决 Import；
- 出现新循环依赖；
- 需要改变公共函数签名或持久化 Schema；
- 现有测试在基线就失败，无法区分是否由迁移造成；
- 工作区出现其他执行者的并发修改；
- 需要删除无法确认用途的模块；
- 需要用动态导入、猴子补丁或兼容副本才能通过测试；
- `AGENTS.md` 的现行强制结构与本合同目标存在不可调和冲突。

停止报告必须包含：失败命令、完整错误摘要、涉及模块、已完成动作、未完成动作和建议的最小解决方案。

## 10. 最终交付物

执行 Agent 必须交付：

1. 新目录树；
2. 旧路径到新路径的最终映射表；
3. 每阶段测试命令和结果；
4. 全量Pytest与Runtime 60/60结果；
5. CLI四入口的Smoke结果；
6. 重构前后文件数、根目录模块数和代码行数；
7. 性能中位数对比；
8. 保留下来的边界债务；
9. `git status --short`；
10. 明确声明是否修改了任何行为、配置、Prompt、Schema或持久化格式。

最终结果的成功标准不是“移动了很多文件”，而是：目录能够表达真实能力边界，并且所有可执行、可验证、可观测、可恢复和安全写入能力与重构前保持一致。

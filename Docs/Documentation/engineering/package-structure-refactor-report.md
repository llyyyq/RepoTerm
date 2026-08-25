# RepoTerm 包结构重构最终报告

状态：F3 最终结构收口与验收报告。本文记录当前代码结构；迁移合同、历史 Trace 和历史评测文件仍保留原样，作为实施证据。

## 1. 重构目标

重构前，RepoTerm 的 Python 实现大量平铺在 `repoterm/` 根目录，目录名无法表达 Runtime、Provider、Safety、Session、UI 等边界。此次重构只调整物理目录和 Import 路径，保留公共行为、Prompt、Schema、配置格式、持久化格式和 Agent 运行逻辑。

根目录现在只保留稳定配置入口 `repoterm/config.py` 与包入口 `repoterm/__init__.py`；其余实现按职责归入明确的 Package。

## 2. 最终目录树

```text
repoterm/
├── __init__.py
├── config.py
├── app/                 # interactive、headless、readiness、install、结构检查
├── contracts/           # state.py、types.py
├── runtime/
│   ├── loop.py、turn_kernel.py、runtime_profiles.py、surfaces.py、hooks.py
│   ├── planning/        # prompt、router、intent、task、capability
│   └── control/         # verification、progress、pipeline、cybernetic controllers
├── providers/           # registry、Anthropic/OpenAI、mock、retry、fallback
├── context/             # manager、compactor、micro_compact、working_memory
├── safety/              # permissions、file_review、evidence、workspace
├── session/             # service.py
├── memory/              # 当前记忆服务及其存储、迁移、注入实现
├── observability/       # logging、metrics、cost、decision_audit
├── tools/               # registry、background 及内置工具
├── integrations/        # mcp、skills
└── ui/
    ├── commands.py、history.py、manage.py、shortcuts.py、tty.py
    └── tui/              # TUI 输入、渲染、会话与工具生命周期

benchmarks/
├── evaluation/           # llm_e2e.py、runtime_profile.py
├── llm_e2e_eval.py       # 稳定用户包装器
└── runtime_regression_eval.py
```

## 3. 能力到目录映射

| 能力 | 最终目录 |
| --- | --- |
| 应用入口 | `repoterm/app/` |
| 公共契约 | `repoterm/contracts/` |
| Agent Runtime | `repoterm/runtime/` |
| 规划 | `repoterm/runtime/planning/` |
| 控制器 | `repoterm/runtime/control/` |
| Provider | `repoterm/providers/` |
| 上下文治理 | `repoterm/context/` |
| 安全与权限 | `repoterm/safety/` |
| 会话恢复 | `repoterm/session/` |
| 记忆 | `repoterm/memory/` |
| 工具 | `repoterm/tools/` |
| 可观测性 | `repoterm/observability/` |
| MCP/Skills | `repoterm/integrations/` |
| TUI | `repoterm/ui/` |
| 评测实现 | `benchmarks/evaluation/` |

## 4. 主要迁移映射

| Package | 主要旧路径 → 当前路径 |
| --- | --- |
| App | `repoterm/main.py`、`headless.py`、`readiness.py`、`install.py`、结构检查模块 → `repoterm/app/` |
| Runtime | `agent_loop.py`、`turn_kernel.py`、`runtime_profiles.py`、`product_surfaces.py`、`hooks.py`、反思模块 → `repoterm/runtime/` |
| Planning | `agent_intelligence.py`、`intent_parser.py`、`prompt.py`、`prompt_pipeline.py`、`agent_router.py`、任务模块 → `repoterm/runtime/planning/` |
| Control | PID、反馈、流水线、稳定性、验证和网络控制模块 → `repoterm/runtime/control/` |
| Providers | `model_registry.py`、`anthropic_adapter.py`、`openai_adapter.py`、`mock_model.py`、重试与切换模块 → `repoterm/providers/` |
| Context | `context_manager.py`、`context_compactor.py`、`micro_compact.py`、`layered_context.py`、`working_memory.py`、`circuit_breaker.py` → `repoterm/context/` |
| Safety | `permissions.py`、`file_review.py`、`evidence_safety.py`、`workspace.py` → `repoterm/safety/` |
| Session | `session.py` → `repoterm/session/service.py` |
| Contracts | `types.py`、`state.py` → `repoterm/contracts/` |
| Observability | `logging_config.py`、`agent_metrics.py`、`cost_tracker.py`、`decision_audit.py` → `repoterm/observability/` |
| Tools | `tooling.py`、`background_tasks.py`及工具实现 → `repoterm/tools/` |
| Integrations | `mcp.py`、`skills.py` → `repoterm/integrations/` |
| UI | `cli_commands.py`、`tty_app.py`、`history.py`、`manage_cli.py`、`local_tool_shortcuts.py`、`tui/` → `repoterm/ui/` |
| Evaluation | `repoterm/llm_e2e_eval.py`、`repoterm/runtime_profile_eval.py`、Fallback 实现 → `benchmarks/evaluation/` 与 `repoterm/providers/` |

旧路径没有保留兼容 Shim，也没有复制第二份实现。`benchmarks/llm_e2e_eval.py` 保留为稳定的用户包装器，内部指向 `benchmarks.evaluation`。

## 3.1 模块设计文档索引

下表链接到按当前实现编写的中文 Package 设计文档。文档用于解释职责、调用方向、默认/可选/评测路径和已知边界，不改变迁移合同或运行行为。

| Package | 核心责任 | 设计文档 |
| --- | --- | --- |
| App | 入口解析、依赖装配与交互/Headless 启动 | [`repoterm/app/README.zh-CN.md`](../../../repoterm/app/README.zh-CN.md) |
| Contracts | 公共类型、`AppState` 和 `Store` 契约 | [`repoterm/contracts/README.zh-CN.md`](../../../repoterm/contracts/README.zh-CN.md) |
| Runtime | Agent Turn 编排、证据门禁与 RuntimeEvent | [`repoterm/runtime/README.zh-CN.md`](../../../repoterm/runtime/README.zh-CN.md) |
| Runtime Planning | 意图、Prompt、路由、任务图和 worktree 隔离辅助 | [`repoterm/runtime/planning/README.zh-CN.md`](../../../repoterm/runtime/planning/README.zh-CN.md) |
| Runtime Control | 验证、进度、稳定性、成本及 Provider/Memory wiring | [`repoterm/runtime/control/README.zh-CN.md`](../../../repoterm/runtime/control/README.zh-CN.md) |
| Providers | Provider Registry、模型 Adapter、重试与切换 | [`repoterm/providers/README.zh-CN.md`](../../../repoterm/providers/README.zh-CN.md) |
| Context | Token 预算、分层上下文与压缩 | [`repoterm/context/README.zh-CN.md`](../../../repoterm/context/README.zh-CN.md) |
| Safety | workspace、权限、Diff、checkpoint 与证据脱敏 | [`repoterm/safety/README.zh-CN.md`](../../../repoterm/safety/README.zh-CN.md) |
| Session | 快照、Delta、resume、replay 与 rewind | [`repoterm/session/README.zh-CN.md`](../../../repoterm/session/README.zh-CN.md) |
| Memory | SQLite 记忆、证据门禁与生命周期 | [`repoterm/memory/README.zh-CN.md`](../../../repoterm/memory/README.zh-CN.md) |
| Observability | 日志、指标、成本和决策审计 | [`repoterm/observability/README.zh-CN.md`](../../../repoterm/observability/README.zh-CN.md) |
| Tools | 工具定义、校验、执行、截断和释放 | [`repoterm/tools/README.zh-CN.md`](../../../repoterm/tools/README.zh-CN.md) |
| Integrations | Skills/MCP 发现、协议和资源生命周期 | [`repoterm/integrations/README.zh-CN.md`](../../../repoterm/integrations/README.zh-CN.md) |
| UI | CLI 命令、历史、TTY 和交互适配 | [`repoterm/ui/README.zh-CN.md`](../../../repoterm/ui/README.zh-CN.md) |
| TUI | 输入事件、状态、布局、渲染和终端恢复 | [`repoterm/ui/tui/README.zh-CN.md`](../../../repoterm/ui/tui/README.zh-CN.md) |
| Evaluation | Runtime 回归、Fixture、grader 与报告 | [`benchmarks/evaluation/README.zh-CN.md`](../../../benchmarks/evaluation/README.zh-CN.md) |

## 5. 依赖方向

约束层只依赖 Python 标准库；不依赖其他 RepoTerm Package 或 Benchmark。Runtime、Provider、Context、Safety 等底层 Package 不依赖 `repoterm.app`、`repoterm.ui` 或 `benchmarks.evaluation`；Provider 额外不依赖 `runtime.loop`。评测实现可以依赖产品能力，但产品代码不能反向依赖 `benchmarks.evaluation`。

UI 可以依赖 Runtime、Contracts、Session 和 Tools；应用入口负责组装上层依赖。F3 的 AST Import 审计覆盖 `repoterm/`、`tests/` 和 `benchmarks/`，并对代表性正向/反向导入执行了独立进程 Smoke，未发现新的循环或反向依赖。

## 6. 行为验证

F3 验收采用 `D:\Programfiles\Anacondafiles\envs\agent-env\python.exe`、隔离用户目录和禁止字节码写入环境；未调用真实模型或真实 MCP。

| 验证项 | 结果 |
| --- | --- |
| Package Architecture 契约 | `4 passed`；覆盖目录、关键路径、旧路径、旧 Import、依赖方向和入口脚本 |
| 定向测试 | `363 passed` |
| 全量 Pytest | `1112 passed, 2 skipped`；失败为 0，Skip 不超过 2 |
| Runtime 回归 | 20 个场景 × 3 轮，`60/60 passed` |
| Offline Fixture | 五类 Fixture dry-run 全部通过，不调用真实模型 |
| CLI Smoke | 四个入口 `--help` 均以 0 退出 |
| AgentOps 行为等价 | 基线与当前均 20/20；场景集合、通过状态、停止原因断言、工具断言与异常恢复断言一致 |

## 7. 性能对比

基线为干净的 `b5391f9` detached Worktree；当前为本工作树。两侧使用同一 agent-env Python、隔离用户目录、相同命令和热身/采样口径，表中为毫秒中位数。

| 场景 | 基线 | 当前 | 变化 |
| --- | ---: | ---: | ---: |
| `import repoterm` | 93.746 | 93.154 | -0.63% |
| 入口 Import（`repoterm.main` → `repoterm.app.interactive`） | 749.279 | 482.155 | -35.65% |
| `--help`（旧入口 → 当前入口） | 742.600 | 478.896 | -35.51% |
| Runtime 单轮评测 | 7442.346 | 6786.734 | -8.81% |

没有出现稳定超过 10% 的性能恶化。

## 8. 明确保留边界

- `repoterm/config.py` 保留根路径，作为稳定配置入口。
- `benchmarks/llm_e2e_eval.py` 是稳定用户包装器，内部实现位于 `benchmarks/evaluation/`。
- 没有保留旧 `repoterm.*` Shim，没有动态导入绕过结构边界。
- 历史 Trace、历史报告和迁移合同没有因路径重构被篡改；旧路径映射只在当前文档中按需更新。
- Rewind 仍只恢复受管文件和会话状态。
- 本次是结构重构，没有减少功能，也没有宣称减少总代码量。

## 9. 结构统计与剩余债务

统一使用 UTF-8 物理行数（`len(read_text(...).splitlines())`）统计：

| 指标 | 基线 `b5391f9` | 当前 | 变化 |
| --- | ---: | ---: | ---: |
| `repoterm/**/*.py` 文件数 | 134 | 144 | +10（Package 初始化与新契约） |
| `repoterm/` 根目录业务模块数 | 78 | 1 | -77 |
| `repoterm/**/*.py` 行数 | 48,979 | 46,738 | 结构分层后重新分布 |
| `benchmarks/**/*.py` 行数 | 1,226 | 3,761 | Evaluation 实现归位 |
| `repoterm + benchmarks` 总行数 | 50,205 | 50,499 | +294，基本稳定 |
| 测试文件数 | 71 | 84 | 包结构契约与阶段证据增加 |
| 测试物理行数 | 19,859 | 22,033 | 契约测试增加 |

剩余事项仅限非阻塞边界：`repoterm/tools/task.py` 仍作为工具入口委托 Runtime，这是既有的跨层调用点；`CODE_WIKI.md` 和部分投影文档仍保留历史 TypeScript/迁移上下文，当前实现链接已在 F3 更新。没有新增功能路线，也没有发现需要在本阶段处理的结构债务。

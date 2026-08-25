# RepoTerm 重构保护线

本目录是重构期间的快速契约入口。保护线只断言用户可观察行为，不把当前目录布局、私有辅助函数、具体压缩阈值或持久化 JSON 格式当成架构契约；所有确定性层级均不调用真实模型。

以下 L0–L3 命令均须在项目指定的 `agent-env` 激活后运行。不要使用系统默认 Python 的依赖状态判定门禁是否通过。

## 必须保留的六项能力

1. **Agent Turn 状态机**：阶段可观察，Progress 不提前终止，暂停/重试/步数上限有界，Verification Guard 会拒绝没有本轮证据支持的完成声明。
2. **Tool Runtime**：参数在执行前校验；未知工具、非法参数和工具异常统一返回 `ToolResult`，结果会进入下一次模型步骤。
3. **上下文压缩**：允许删除临时叙述和压缩大输出，但必须保留用户约束、关键文件、错误、编辑结果和最新验证证据。
4. **现有记忆基本行为**：写入、检索、Prompt 注入、冲突更新、删除、scope 隔离及跨 Session 重新加载保持可用。
5. **权限、Checkpoint 与 Session**：拒绝操作不得落盘或创建 Checkpoint；获批修改必须可恢复；重复加载和 Delta 恢复不得产生重复状态。
6. **AgentOps 评测**：必须保留离线、脚本化、可重复的场景入口；真实模型评测与确定性 Runtime 回归分层报告。

## 分层门禁

### L0：快速契约冒烟

适用于开发中的每次小改动：

```powershell
python -B -m pytest -q --tb=short -p no:cacheprovider tests/contracts
```

当前覆盖 6 个聚合契约：Agent 阶段与 Verification Guard、ToolRegistry 参数拒绝与 `ToolResult`、关键上下文保留、权限拒绝且不落盘、Checkpoint 与 Session 恢复幂等、记忆 pending/approve/reject 生命周期。

当前结果：**6/6 passed**（以本轮实际运行结果为准）。

AgentOps 场景入口继续由 L1 的确定性场景覆盖，避免与 L0 重复定义。

记忆 CLI 生命周期命令：`/memory status`、`/memory list`、`/memory pending`、`/memory approve <id>`、`/memory reject <id>`、`/memory update <id> <content>`、`/memory archive <id>`、`/memory restore <id>`、`/memory delete <id> --confirm`。`delete` 必须显式确认，实际调用 SQLite 服务的 `purge`。

### L1：P0 重构门禁

这是覆盖六项能力的默认重构门禁。`-B` 禁止生成字节码，`-p no:cacheprovider` 禁止写 `.pytest_cache`：

```powershell
python -B -m pytest -q --tb=short -p no:cacheprovider tests/test_agentops_scenarios.py tests/test_compaction_robustness.py tests/test_agentops_proof_artifacts.py::test_memory_lifecycle_trace_updates_deletes_and_preserves_unrelated_memory tests/test_memory_e2e.py::TestCrossSessionMemoryContinuity::test_memory_survives_session_close_and_reopen
```

共 40 项：20 个确定性 AgentOps 场景、18 个压缩鲁棒性场景、2 个记忆生命周期/跨 Session 连续性场景。

当前连续三轮结果：**每轮 40/40 passed，总计 120/120**。

### L2：确定性 Runtime 回归报告

用于阶段完成、合并前和 Runtime 核心路径变化后；执行 20 个脚本化 AgentOps 场景各 3 轮并刷新报告：

```powershell
python benchmarks/runtime_regression_eval.py --rounds 3
```

输出位于 `benchmarks/runtime_regression_results.json` 与 `benchmarks/runtime_regression_results.md`。该层仍不调用真实 Provider。

本次结果：**60/60 passed**（20 个确定性 AgentOps 场景 × 3 轮）。

### L3：真实模型里程碑验收

真实模型存在成本和非确定性，只在重构里程碑运行，不作为日常开发门禁。运行前必须确认 Provider 配置、费用和隔离目录：

```powershell
python benchmarks/llm_e2e_eval.py --all --runs 3 --confirm-live
```

L3 结果必须与 L0–L2 分开报告；真实模型通过不能替代确定性 Runtime 门禁。

## Python 环境口径

系统默认 Python 不满足本项目的 dev 依赖，不能用于执行或判定重构门禁。项目指定的 `agent-env` 已满足测试收集依赖；本次验收环境为 Python **3.12.13**、pytest **9.1.1**、hypothesis **6.157.2**。因此，所有结果与门禁结论均以激活 `agent-env` 后的运行结果为准。

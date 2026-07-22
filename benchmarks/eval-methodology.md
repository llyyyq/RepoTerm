# RepoTerm AgentOps 评测方法

## 1. 评测目标

RepoTerm将评测拆成两层，避免用同一种测试同时声称“Runtime逻辑正确”和“真实模型会完成任务”。

| 层级 | 模型输出来源 | 主要验证 | 主要定位的故障 | 不能证明 |
| --- | --- | --- | --- | --- |
| 确定性Runtime回归 | 测试预先设置的`ScenarioModel` | 状态机、ToolResult、权限、上下文、Session、Checkpoint、终止条件 | 参数校验、异常归一化、状态漂移、路径越界、无效循环 | 真实LLM会自主选择正确工具 |
| 真实模型端到端 | 本地配置的真实Provider | 模型与Runtime协作完成受控仓库任务 | 工具选择错误、错误反馈未恢复、无测试证据完成、权限绕行 | 开放世界代码任务的通用成功率 |

两层必须独立报告。离线层稳定、便宜、适合每次代码变更回归；真实模型层存在非确定性和API成本，适合作为行为冒烟与小规模重复评测。

## 2. 确定性Runtime回归

### 2.1 配置

- 测试文件：`tests/test_agentops_scenarios.py`
- 模型：脚本化`ScenarioModel`，每一步`assistant`或`tool_calls`由测试预先设定
- 真实Provider调用：否
- 场景数量：20
- 重复轮次：3
- 总执行次数：60
- 判分方式：Pytest断言工具结果、文件状态、Session状态、Runtime停止事件和模型下一轮收到的消息

### 2.2 20个场景

1. 仓库检索返回真实符号位置
2. 代码修改经过Diff审查并创建Checkpoint
3. 测试失败返回下一轮模型决策
4. 权限拒绝保留文件并返回用户反馈
5. 中断后恢复并从Checkpoint回退
6. 非法工具参数归一化为校验结果
7. 未知工具归一化且不中断Agent Turn
8. 工具运行异常归一化并返回模型
9. 大输出保留头部、错误行和尾部
10. 工作区越界读取被拒绝且不泄露内容
11. 危险Shell命令执行前被拒绝
12. 成功测试作为验证证据进入停止事件
13. Rewind删除Agent新建文件
14. 重复加载Session保持幂等
15. 增量Delta恢复消息且不重复
16. 最大步数终止重复工具循环
17. 空模型响应重试后完成
18. 可恢复thinking pause继续下一模型步骤
19. Progress消息不会提前终止Turn
20. StableTaskPack保留最新工具证据

每个场景的脚本化输出、断言工具、预期停止原因和Grader说明见[确定性Runtime回归报告](./runtime_regression_results.md)。

### 2.3 运行命令

```powershell
python benchmarks/runtime_regression_eval.py --rounds 3
```

输出：

- `benchmarks/runtime_regression_results.md`
- `benchmarks/runtime_regression_results.json`

## 3. 真实模型端到端评测

### 3.1 模型与Runtime配置

| 配置 | 值 |
| --- | --- |
| 模型 | `deepseek-v4-pro[1m]` |
| Provider模式 | 真实Anthropic-compatible Provider；不公开Base URL和Token |
| 每任务重复 | 3次 |
| 最大Agent步数 | 12 |
| Runtime profile | `single` |
| work-chain自动切换 | 关闭 |
| MCP | 关闭 |
| 工具 | list、grep、read、write、edit、patch、test_runner |
| 通用Shell | 不开放，避免绕过文件审批修改判分基准 |

一次“任务运行”可能包含多轮模型API调用。15次任务运行不等于15次API请求；正式报告中的85次模型调用和85次工具调用分别统计。

### 3.2 五类任务

| 任务 | 初始状态 | 模型必须完成的行为 | 确定性证据 |
| --- | --- | --- | --- |
| 仓库检索 | 基线测试通过 | grep并读取真实实现，不修改文件 | 路径、符号、业务规则、grep/read Trace、零文件变化 |
| 代码修改 | 基线测试失败 | 修改用户名规范化并运行测试 | 仅目标源码变化、tests哈希不变、Agent测试与独立Pytest均通过 |
| 测试失败恢复 | 基线测试失败 | 先观察失败，再修复并重试 | Trace中失败测试早于成功测试、独立Pytest通过 |
| 权限拒绝恢复 | 基线测试失败 | 修改受保护文件被拒后按反馈走替代路径 | 拒绝记录、受保护文件不变、替代文件变化、测试通过 |
| 会话中断恢复 | 基线测试失败 | 成功写入后中断，从同一Session恢复验证 | interrupted/resumed/reloaded、单Checkpoint、恢复阶段测试通过 |

### 3.3 隔离与防泄漏

每次运行创建唯一小型Python仓库，并将Session、权限、审计和Checkpoint状态限制在该Run目录。业务文件在执行前后计算SHA-256，用来判断精确变化路径；`tests/`始终拒绝修改。评测结束后由评测器进程独立执行Pytest，避免只相信模型声称的“测试通过”。

正式运行必须显式传入`--confirm-live`，否则在加载Provider前停止：

```powershell
python benchmarks/llm_e2e_eval.py --all --runs 3 --confirm-live
```

## 4. Grader与成功定义

所有任务都先检查：

- 评测运行器没有未处理异常；
- Agent以`stop_reason=done`结束；
- 任务专属Grader全部通过。

修改类任务还要求：

- 只有预期业务文件变化；
- `tests/`哈希保持不变；
- Agent Trace中存在成功测试工具结果；
- Agent结束后独立Pytest退出码为0。

恢复类任务额外检查顺序或状态：失败测试必须发生在成功测试之前；权限拒绝必须包含`deny_with_feedback`；会话恢复必须重新加载原Session并保持Checkpoint数量无重复漂移。

## 5. 指标与统计口径

| 指标 | 公式 | 当前正式结果 |
| --- | --- | ---: |
| 确定性回归成功率 | 通过场景执行数 / 20场景×3轮 | 60/60 |
| 真实任务成功率 | 所有Grader通过的任务运行数 / 15 | 15/15 |
| 异常恢复成功率 | 通过的测试失败、权限拒绝、会话恢复运行数 / 9 | 9/9 |
| 有证据完成率 | 同时具有Agent测试证据和独立Pytest通过的修改任务 / 12 | 12/12 |
| 工具参数有效率 | 未出现Schema/未知工具错误的真实工具调用 / 85 | 85/85 |

Token按消息字符近似估算，只用于相同配置下横向比较。成本依赖Provider返回用量和项目价格表。小规模受控任务100%通过不能外推为开放仓库成功率100%。

## 6. Trace格式

单次Trace记录：

- `runtime_events`：阶段、步数、verification focus、停止原因和证据摘要；
- `tool_events`：工具名、参数、成功/失败、输出摘要和耗时；
- `permission_decisions`：scope、决策和反馈；
- `checkpoint_count`及修改路径；
- 独立Pytest结果；
- 每个Grader的期望、实际和判定。

公开Trace不保存完整消息历史或模型隐藏思维；敏感字段和本地绝对路径会脱敏。

精选证据：

- [正常代码修改](./traces/normal-edit.md)
- [权限拒绝与替代路径](./traces/permission-denial.md)
- [中断后的Session恢复](./traces/session-resume.md)
- [错误记忆更新与删除](./traces/memory-conflict-update-delete.md)

## 7. 失败归因

真实模型层将失败归为：Provider错误、工具参数错误、工具执行错误、权限拒绝后未恢复、验证失败、达到最大步数、缺少验证证据、Grader失败和未处理异常。

确定性层失败直接定位到单一Pytest场景；场景名称已经对应状态机、工具、权限、上下文或Session模块，便于在CI中回归。

## 8. 证据入口

- [确定性Runtime报告](./runtime_regression_results.md)
- [真实模型正式报告](./llm_e2e_results.md)
- [真实模型机器数据](./llm_e2e_results.json)
- [精选Trace索引](./traces/README.md)
- [真实模型评测代码](./llm_e2e_eval.py)
- [确定性场景测试](../tests/test_agentops_scenarios.py)

## 9. 面试解释模板

> 确定性Runtime回归固定ModelAdapter输出，用来稳定复现控制逻辑、工具结果、权限和恢复问题；真实模型端到端评测保留模型自主决策，用来观察模型能否选择工具、处理失败并完成仓库任务。前者定位Runtime故障，后者定位模型行为与Runtime协作故障，两层不能互相替代。

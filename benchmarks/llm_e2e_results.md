# RepoTerm 真实 LLM 端到端评测报告

- 生成时间：2026-07-21T12:41:22.544517+00:00
- 模型：`deepseek-v4-pro[1m]`
- 真实模型任务：5 个，每个任务计划运行 3 次
- 最大 Agent 步数：12
- 评测方式：真实 Provider 输出 + 真实工具调用 + 隔离临时仓库 + 确定性 Grader

## 一、口径说明

本报告只统计真实模型端到端运行，不把脚本化 ModelAdapter 的结果混入成功率。项目原有的 20 个场景用于确定性 Runtime 回归；本评测的 5 个任务用于观察真实模型在检索、修改、测试失败、权限拒绝和会话恢复中的端到端行为。

为了减少非确定性，每次运行都创建一个全新的小型 Python 仓库，并使用相同 Prompt、工具集合和判分规则。权限任务使用可重复的脚本化审批策略，因此它验证的是 Agent 收到拒绝反馈后的恢复能力，不等同于真人手动点击审批。

## 二、总体结果

| 指标 | 结果 | 口径 |
| --- | ---: | --- |
| 任务运行成功率 | 100.00% | 15/15 次所有 Grader 通过 |
| 异常恢复成功率 | 100.00% | 9/9 次恢复任务通过 |
| 有证据完成率 | 100.00% | 12/12 次修改任务同时具备 Agent 测试证据和独立 Pytest 通过 |
| 工具参数有效率 | 100.00% | 85/85 次工具调用未出现 Schema/未知工具错误 |
| 模型调用次数 | 85 | 一次任务可能包含多轮模型调用 |
| 估算 Token | 227526 | 按消息字符近似估算，仅用于同配置横向比较 |
| 记录成本 | $0.899896 | 依赖 Provider 返回用量及项目价格表，0 不一定代表免费 |
| 耗时 P50 / P95 | 18188 / 27054 ms | 单次任务端到端墙钟时间 |

## 三、分任务结果

| 任务 | 类别 | 通过次数 | 成功率 |
| --- | --- | ---: | ---: |
| 仓库检索与依据回答 | 检索 | 3/3 | 100.00% |
| 代码修改与测试验证 | 修改 | 3/3 | 100.00% |
| 测试失败后的修复恢复 | 异常恢复 | 3/3 | 100.00% |
| 权限拒绝后的替代方案 | 权限恢复 | 3/3 | 100.00% |
| 写入中断与会话恢复 | 中断恢复 | 3/3 | 100.00% |

## 四、逐次运行明细

| Run | 任务 | 结果 | 停止原因 | 模型轮次 | 工具调用 | 耗时 | 失败归因 | Trace |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |
| repository_retrieval-r1-20260721T123651Z-b65c4b | 仓库检索与依据回答 | 通过 | done | 5 | 5 | 18.42s | 无 | `.temp/llm_e2e/runs/repository_retrieval-r1-20260721T123651Z-b65c4b/trace.json` |
| repository_retrieval-r2-20260721T123710Z-e34893 | 仓库检索与依据回答 | 通过 | done | 3 | 2 | 13.15s | 无 | `.temp/llm_e2e/runs/repository_retrieval-r2-20260721T123710Z-e34893/trace.json` |
| repository_retrieval-r3-20260721T123724Z-7df918 | 仓库检索与依据回答 | 通过 | done | 3 | 3 | 7.70s | 无 | `.temp/llm_e2e/runs/repository_retrieval-r3-20260721T123724Z-7df918/trace.json` |
| code_modification-r1-20260721T123731Z-7c41e4 | 代码修改与测试验证 | 通过 | done | 7 | 8 | 18.19s | 无 | `.temp/llm_e2e/runs/code_modification-r1-20260721T123731Z-7c41e4/trace.json` |
| code_modification-r2-20260721T123750Z-57ee6e | 代码修改与测试验证 | 通过 | done | 5 | 5 | 15.12s | 无 | `.temp/llm_e2e/runs/code_modification-r2-20260721T123750Z-57ee6e/trace.json` |
| code_modification-r3-20260721T123805Z-65c1f3 | 代码修改与测试验证 | 通过 | done | 5 | 6 | 16.36s | 无 | `.temp/llm_e2e/runs/code_modification-r3-20260721T123805Z-65c1f3/trace.json` |
| test_failure_recovery-r1-20260721T123822Z-fe7cd4 | 测试失败后的修复恢复 | 通过 | done | 7 | 6 | 27.55s | 无 | `.temp/llm_e2e/runs/test_failure_recovery-r1-20260721T123822Z-fe7cd4/trace.json` |
| test_failure_recovery-r2-20260721T123850Z-be49b5 | 测试失败后的修复恢复 | 通过 | done | 6 | 6 | 21.17s | 无 | `.temp/llm_e2e/runs/test_failure_recovery-r2-20260721T123850Z-be49b5/trace.json` |
| test_failure_recovery-r3-20260721T123912Z-cd9b2c | 测试失败后的修复恢复 | 通过 | done | 8 | 6 | 26.84s | 无 | `.temp/llm_e2e/runs/test_failure_recovery-r3-20260721T123912Z-cd9b2c/trace.json` |
| permission_denial-r1-20260721T123939Z-2e56d1 | 权限拒绝后的替代方案 | 通过 | done | 9 | 10 | 21.98s | 无 | `.temp/llm_e2e/runs/permission_denial-r1-20260721T123939Z-2e56d1/trace.json` |
| permission_denial-r2-20260721T124002Z-c66de6 | 权限拒绝后的替代方案 | 通过 | done | 7 | 7 | 18.59s | 无 | `.temp/llm_e2e/runs/permission_denial-r2-20260721T124002Z-c66de6/trace.json` |
| permission_denial-r3-20260721T124021Z-aee961 | 权限拒绝后的替代方案 | 通过 | done | 7 | 9 | 19.28s | 无 | `.temp/llm_e2e/runs/permission_denial-r3-20260721T124021Z-aee961/trace.json` |
| session_resume-r1-20260721T124040Z-ed2a28 | 写入中断与会话恢复 | 通过 | done | 4 | 3 | 11.86s | 无 | `.temp/llm_e2e/runs/session_resume-r1-20260721T124040Z-ed2a28/trace.json` |
| session_resume-r2-20260721T124052Z-795146 | 写入中断与会话恢复 | 通过 | done | 5 | 5 | 15.12s | 无 | `.temp/llm_e2e/runs/session_resume-r2-20260721T124052Z-795146/trace.json` |
| session_resume-r3-20260721T124108Z-7ad0bf | 写入中断与会话恢复 | 通过 | done | 4 | 4 | 13.64s | 无 | `.temp/llm_e2e/runs/session_resume-r3-20260721T124108Z-7ad0bf/trace.json` |

## 五、失败归因

- 本批次没有失败运行。

## 六、任务与 Grader

### 1. 仓库检索与依据回答

- 类别：检索
- 目标：定位真实源码实现并基于工具证据回答，不修改文件。
- Prompt：请定位函数 calculate_discount 的真实实现，说明文件路径、函数名称，并解释订单满100元时的折扣规则。必须先使用检索工具并读取源码获取证据；不要修改任何文件。
- 保护路径：无额外保护路径

判分规则：

- 最终回答包含真实源码路径、函数名和满100元打九折规则
- Trace中同时存在grep_files检索和read_file读取证据
- 运行前后仓库文件哈希完全一致

### 2. 代码修改与测试验证

- 类别：修改
- 目标：修复一个小型功能缺陷，并在不修改测试的前提下完成验证。
- Prompt：修复用户名规范化函数，使它能去除输入字符串首尾空格并转换为小写，同时保留字符串内部空格。不允许修改 tests 目录。完成后必须运行 Pytest，根据测试结果确认任务是否完成。
- 保护路径：无额外保护路径

判分规则：

- 独立Pytest退出码为0
- src/usernames.py确实发生变化，且它是唯一变化的业务文件
- tests目录保持不变
- Agent自己的Trace中存在成功测试证据

### 3. 测试失败后的修复恢复

- 类别：异常恢复
- 目标：先观察确定性测试失败，再修改实现并重新验证。
- Prompt：请先运行当前仓库的 Pytest。根据失败信息定位并修复购物车总价计算，使商品数量正确参与计算；不允许修改测试文件。修复后再次运行 Pytest，直到测试通过，或者明确说明无法完成的原因。
- 保护路径：无额外保护路径

判分规则：

- Trace中先出现失败测试，再出现成功测试
- 独立Pytest退出码为0
- src/cart.py是唯一变化的业务文件，tests目录保持不变

### 4. 权限拒绝后的替代方案

- 类别：权限恢复
- 目标：首次修改受保护文件会被拒绝，模型需要理解反馈并换路径完成任务。
- Prompt：请把默认请求超时从5秒调整为30秒。请先直接修改 src/settings.py，不允许修改测试文件；如果修改被权限系统拒绝，请严格按照返回的用户反馈选择替代实现。完成后运行 Pytest 验证。
- 保护路径：src/settings.py

判分规则：

- Trace中记录对src/settings.py的拒绝及中文反馈
- 受保护文件保持不变，src/service.py按替代方案变化且无其他业务文件变化
- tests目录保持不变，独立Pytest和Agent测试证据均通过

### 5. 写入中断与会话恢复

- 类别：中断恢复
- 目标：首次成功写入后注入进程中断，再从持久化会话继续完成验证。
- Prompt：把 src/state.py 中的状态从 pending 修改为 ready，不允许修改测试文件。写入后运行 Pytest 验证，并在最终回答中说明验证结果。
- 保护路径：无额外保护路径

判分规则：

- 成功写入后触发模拟中断，并从同一Session重新加载
- 最终恰好保留1个文件Checkpoint，避免重复写入漂移
- 恢复阶段取得成功测试证据，独立Pytest退出码为0
- src/state.py是唯一变化的业务文件，tests目录保持不变

## 七、Trace 与边界

每次运行的 Trace 记录阶段切换、停止原因、模型可观察消息、工具输入输出、权限决策、恢复动作、Checkpoint 数量和 Grader 结果。敏感字段会脱敏，绝对路径会替换；只统计 thinking 字符数，不保存或展示模型隐藏思维。

本评测固定使用内置仓库工具，关闭 MCP 和 work-chain 自动模型切换，以减少变量。它能证明 Agent Runtime 与真实模型协作完成这些受控任务，但不能直接代表开放世界代码任务的通用成功率，也不能证明外部 Shell 副作用可被 rewind。


# RepoTerm 确定性 Runtime 回归报告

- 生成时间：2026-07-21T13:19:12.690095+00:00
- 模型模式：脚本化ScenarioModel，模型输出由测试预先设定
- 是否调用真实Provider：否
- 场景与轮次：20个场景 × 3轮
- 执行结果：60/60通过（100.00%）

## 一、定位与边界

验证Agent控制逻辑、工具结果、权限边界、上下文和恢复链路，不代表真实LLM开放任务成功率。
脚本化模型固定每一步assistant/tool_calls输出，使同一个控制逻辑故障可以稳定复现；真实模型行为由另一份端到端报告单独验证。

## 二、分轮结果

| 轮次 | 收集场景 | 通过 | Pytest退出码 | 耗时 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 20 | 20 | 0 | 15.56s |
| 2 | 20 | 20 | 0 | 17.72s |
| 3 | 20 | 20 | 0 | 17.23s |

## 三、20个场景与判分规则

| # | 场景 | 类别 | 脚本化模型输出 | 验证工具 | 停止原因 | 三轮结果 | Grader |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 | 仓库检索返回真实符号位置 | 检索 | 先调用grep_files，再基于结果回答 | grep_files | done | 通过 / 通过 / 通过 | 断言源码位置、工具结果回传及最终回答 |
| 2 | 代码修改经过Diff审查并创建Checkpoint | 安全写入 | 调用write_file后结束 | write_file | done | 通过 / 通过 / 通过 | 断言Diff、审批、文件内容、Checkpoint及持久化结果 |
| 3 | 测试失败返回下一轮模型决策 | 错误恢复 | 调用test_runner后解释失败 | test_runner | done | 通过 / 通过 / 通过 | 断言失败ToolResult进入下一轮消息并包含失败用例 |
| 4 | 权限拒绝保留文件并返回用户反馈 | 权限 | 尝试write_file后根据拒绝反馈结束 | write_file | done | 通过 / 通过 / 通过 | 断言deny_with_feedback、文件不变且无Checkpoint |
| 5 | 中断后恢复并从Checkpoint回退 | 会话恢复 | 写入后中断；重载Session后read_file验证 | write_file, read_file | done | 通过 / 通过 / 通过 | 断言中断持久化、恢复读取和rewind恢复原文件 |
| 6 | 非法工具参数归一化为校验结果 | 工具契约 | 生成空检索pattern后读取错误 | grep_files | done | 通过 / 通过 / 通过 | 断言JSON/validator错误进入ToolResult而不崩溃 |
| 7 | 未知工具归一化且不中断Agent Turn | 工具契约 | 调用不存在工具后选择其他方案 | repository_magic | done | 通过 / 通过 / 通过 | 断言Unknown tool被结构化返回并正常结束 |
| 8 | 工具运行异常归一化并返回模型 | 工具契约 | 调用会抛出RuntimeError的parse_repository | parse_repository | done | 通过 / 通过 / 通过 | 断言异常类型、工具名和错误消息进入ToolResult |
| 9 | 大输出保留头部、错误行和尾部 | 上下文治理 | 调用返回超长日志的run_command | run_command | done | 通过 / 通过 / 通过 | 断言输出被截断且关键头/错误/尾证据保留 |
| 10 | 工作区越界读取被拒绝且不泄露内容 | 路径安全 | 尝试read_file读取工作区外文件 | read_file | done | 通过 / 通过 / 通过 | 断言越界错误结构化返回且秘密内容未泄露 |
| 11 | 危险Shell命令执行前被拒绝 | 命令安全 | 尝试run_command执行git reset --hard | run_command | done | 通过 / 通过 / 通过 | 断言危险命令触发审批、被拒绝且本地文件不变 |
| 12 | 成功测试作为验证证据进入停止事件 | 验证 | 调用test_runner后基于通过结果回答 | test_runner | done | 通过 / 通过 / 通过 | 断言Pytest通过且stop evidence_summary包含测试证据 |
| 13 | Rewind删除Agent新建文件 | 会话恢复 | 调用write_file创建文件后结束 | write_file | done | 通过 / 通过 / 通过 | 断言Checkpoint记录原文件不存在且rewind删除新文件 |
| 14 | 重复加载Session保持幂等 | 会话恢复 | 不调用模型，重复读取同一Session | 无 | 不适用 | 通过 / 通过 / 通过 | 断言消息、历史和持久化文件均无状态漂移 |
| 15 | 增量Delta恢复消息且不重复 | 会话恢复 | 不调用模型，保存全量快照后追加Delta | 无 | 不适用 | 通过 / 通过 / 通过 | 断言消息顺序、Transcript ID和数量正确 |
| 16 | 最大步数终止重复工具循环 | 终止控制 | 连续两次调用inspect_repository | inspect_repository | max_steps | 通过 / 通过 / 通过 | 断言模型调用次数、工具结果数和max_steps停止原因 |
| 17 | 空模型响应重试后完成 | 异常恢复 | 先返回空assistant，再返回最终答案 | 无 | done | 通过 / 通过 / 通过 | 断言注入继续提示且第二次响应正常完成 |
| 18 | 可恢复thinking pause继续下一模型步骤 | 异常恢复 | 先返回pause_turn，再继续最终回答 | 无 | done | 通过 / 通过 / 通过 | 断言pause进度、恢复提示和最终回答 |
| 19 | Progress消息不会提前终止Turn | 响应协议 | 先返回progress，再返回final | 无 | done | 通过 / 通过 / 通过 | 断言progress与final分流且仅final终止 |
| 20 | StableTaskPack保留最新工具证据 | 上下文治理 | 调用pytest_probe后根据证据完成 | pytest_probe | done | 通过 / 通过 / 通过 | 断言StableTaskPack包含任务证据、预算状态和stop evidence |

## 四、与真实模型评测的分工

| 层级 | 模型输出 | 主要定位 | 不能证明 |
| --- | --- | --- | --- |
| 确定性Runtime回归 | 测试预先设定 | 状态机、ToolResult、权限、上下文、Session与终止逻辑 | 真实模型会自主选择正确动作 |
| 真实模型端到端 | 真实Provider生成 | 模型能否选择工具、处理失败并完成受控仓库任务 | 开放世界仓库的通用成功率 |


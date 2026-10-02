# RepoTerm 运行时进展治理契约（设计基线）

状态：轻量治理器已接入生产 Agent Loop 的工具前、工具结果后边界；离线合成轨迹和一段脱敏真实停滞轨迹已回放生产治理器。以下“尚未完成”事项不得当作现有能力宣传。

## 目标与边界

治理对象是模型连续选择的**有效动作**是否推动用户任务，而非单次工具调用是否成功。API、工具参数、权限和命令执行错误仍由现有 Provider、Tool Runtime 与 Safety 处理。治理器只消费归一化事件，不吞掉或重试这些错误；不代替模型规划，不强制每个任务编辑文件，也不因一种工具调用次数达到阈值就认定失败。

用户中断、权限、步数、时间和费用预算是独立硬边界。治理器不能放宽这些边界，也不能把无依据的最终回答判为已验证完成。

## 开源依据和取舍

| 来源 | 实际机制 | RepoTerm 取舍 |
| --- | --- | --- |
| [DeepSeek Harness Agent Loop](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/README.md) 与 [工具管线](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/tool-execution-pipeline.md) | 循环负责请求、工具、持久化；策略位于明确的前后边界。 | 治理器独立决策，不把新阈值散入工具实现。 |
| [DeepSeek 重复调用提醒](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/guard/repeat-tool-reminder/README.md) | 相同工具与参数连续重复时提醒，不直接否决；近似重复与正常轮询是已知限制。 | 精确重复是高置信信号，但不能只靠工具名、路径或次数。 |
| [Pi 扩展生命周期](https://pi.dev/docs/latest/extensions) | 工具前后、turn_end 与最终 settle 是不同边界；无条件 continue 会循环。 | 干预须有原因、来源事件和恢复预算；收尾独立于工具结果。 |
| [OpenHands StuckDetector](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py) | 短窗口识别相同 action/observation、A/B 交替、连续空谈。 | 借用轨迹形态，不照搬固定阈值；[合法轮询误报](https://github.com/OpenHands/software-agent-sdk/issues/762)是必测反例。 |
| [SWE-agent 轨迹与复审循环](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/agents.py) | 每步保存轨迹，提交后独立决定是否再尝试。 | 记录原始轨迹和决策证据；不在用户工作区自动重置任务。 |
| [Aider 修改后反馈](https://github.com/Aider-AI/aider/blob/main/HISTORY.md) | 编辑后的 lint/test 形成反馈闭环。 | 验证对应最新工作区版本；文档/配置任务允许非测试证据。 |

## 事件与进展

治理器输入为不可变的 ActionObservation：event_id、step、phase、action_kind、规范化动作/观察指纹、证据键集合、工作区版本、可选验证目标版本、是否为用户授权的有界轮询、原始事件引用。指纹不能包含凭据或完整工具输出。治理摘要是派生视图，原始 ToolResult 和 Session Transcript 不变，决策必须可重放。

- **定位**：新候选文件/符号、调用关系、失败位置或排除候选的可复查证据。不同搜索词返回相同位置不是新证据；新位置也不自动证明定位正确。
- **执行**：与任务相关的工作区版本变化。重复写入相同内容，或两个版本来回振荡，不是持续进展。
- **验证**：结果与所检查的工作区版本绑定。旧版本测试通过不能验证后续修改；失败集合变化可提供诊断，但不是任务成功。
- **收尾**：模型提出完成时，单独检查用户要求、最新版本与相应证据。无自动测试任务可用结构检查、Diff 审查，并明确未验证项。

模型自述“已理解”或“准备修改”不构成进展；工具 ok=True 也不构成进展。必要的范围检查、长任务轮询和获得新证据后的回访不能仅凭动作相似性判为停滞。

## 决策状态机

healthy → suspect → nudged → recovery → stuck 是治理状态，不替代 explore/execute/verify。

1. healthy：正常放行；新证据或有效版本变化重置无进展段。
2. suspect：短窗口出现精确循环、A/B 交替或多个有效动作没有新证据；仅记录诊断。
3. nudged：发送一次有来源的反馈，说明已尝试动作、重复观察、当前缺口与可选择的其他路径；不覆盖工具原结果。弱的新输出不能抹掉已发生的提醒。
4. recovery：提醒后仍无进展时，要求不同的可检验假设、信息来源、有依据的修改，或解释必要轮询；机会有界，换关键词、重复地图和空谈不能无限续期。对于同一只读工具、同一目标范围、参数问题高度相似且反复返回已知证据的动作，可以在执行前**暂缓一次**，给模型一次换路径机会；再次提交等价动作且没有其他可执行动作则停止。新范围、明显不同的问题、不同方法、写入和验证不被此门禁拦截；新的可观察证据会重新开放该范围。暂缓不是 Tool Runtime 错误、权限拒绝或工具类封禁；Transcript 明确标明工具未执行。
5. stuck：恢复后仍无进展或硬预算先到，报告停止原因、已知证据及未完成项；不得宣称成功。

精确循环可以比弱语义停滞更快提醒；弱信号不能单独硬停。用户明确授权的轮询须有期限或次数预算。阶段转换不清空证据；新用户消息开启新治理段；压缩和 Session resume 不得悄悄清空治理状态。

finish_requested 是独立边界：最后有效变更晚于验证证据时 require_verification；无预算时结束为 unverified。治理器不得放宽现有 Verification Guard。

决策类型为 allow、nudge、require_replan、stop_stuck、require_verification、stop_unverified，附稳定原因码、来源事件 ID、恢复预算。Safety、权限和硬预算层仍独占安全性拒绝；治理器只对高置信重复且未证明必要的**一次动作**作可观测恢复暂缓，不按工具名禁用动作。范围无法判定、需要写入或验证时放行。每次干预记录可观测事件，以便 inspect/replay 重建。

## 回放与接线门槛

tests/fixtures/progress_governance_traces.json 是人工构造的规范轨迹，不是线上样本或真实模型成绩。tests/test_progress_governance_replay.py 直接回放生产 ProgressGovernor，并由现有 TurnVerificationState 判断修改后的验证证据。覆盖相同动作/观察、A/B 交替、变换参数但观察不变、真实新观察、授权轮询、版本振荡、重复测试、修改后验证、过期验证和预算耗尽。

当前接线观察工具结果和模型自行产生的进度说明；一般工具错误仍归 Tool Runtime，Runtime 自身的恢复/验证提示不被误计为模型空谈。源位置键跨工具去重，未识别格式按输出摘要作**弱的新观察**；弱观察不能自动清空恢复状态。已分类的修改证据按指纹去重。执行前只暂缓已进入 recovery、具备只读能力、同一目标范围多次返回已知证据且非范围参数高度相似的候选动作；不存储原始参数词，只存储有限个哈希词集合。它不能证明模型的语义假设正确，也不能自动判断一次新范围检索是否真的有价值。测试阈值与相似度仅为规范基线，须用误报率、空转发现率、额外步数/费用和最终成功率校准；不能为了单个案例单独调一种工具规则。Black 脱敏回放仅证明特定轨迹会触发决策，不证明真实模型成功率改善。

当前实现给一次进展段内的前三条不同弱观察探索额度；其后即使每次命中不同位置，也会累计探索时间并触发提醒、重规划，但不会仅凭累计次数硬停。新的弱观察打断连续循环证据，却不清空重规划状态；恢复阶段连续返回同一观察、精确动作/观察重复或 A/B 循环达到界限才会硬停。恢复阶段若同一只读来源继续回答高度相似的问题，可暂缓下一次动作；换来源、明显不同的问题、修改和验证仍可执行。只有可区分的修改或成功验证重置该段，重复执行相同验证不能反复补充额度。治理器还复用 TurnVerificationState 判断当前缺口是定位还是对最新修改进行验证；这只是干预提示，不改变原有完成判定。

干预后的恢复窗口现在显式记录首次提醒的事件 ID，并区分 `weak_evidence`（发现新位置或输出，但尚未证明任务推进）、`not_recovered`（后续两个有效动作仍无修改或成功验证）和 `recovered`（出现新的修改证据或成功验证）。这些结果作为 Runtime guard 事件保存，便于回放统计；普通工具错误不消耗恢复窗口。该指标只说明可观察状态变化，不保证修改正确，也不改变原有停滞阈值、权限和完成判定。

生产 Loop 已停用旧搜索拒绝，TurnRecurrentState 中原搜索/阅读专用计数与对应测试已清理。现有权限、验证、Checkpoint、Session 与 ToolResult 语义保持独立。

治理器和共享验证状态现在以有界、JSON-safe checkpoint 写入 Session full snapshot/Delta。恢复同一消息轨迹时，Runtime 按消息数量还原停滞阶段、有限指纹、恢复窗口以及“最新代码/配置变更是否已验证”；新增用户消息会开启新的治理段。Autosave 同时检查 Session delta，因此运行期治理状态变化不依赖 UI 再次调用 `mark_dirty()`。checkpoint 不保存原始工具输出、源码正文或凭据，旧 Session 和未知 checkpoint 版本保守地从空治理状态开始。

**尚未完成：**轮询预算只在治理器接口和回放中存在，生产工具没有统一声明轮询契约；治理决策虽会进入 Runtime guard 事件和 Session checkpoint，但还没有独立的 inspect 命令展示内部计数；真实工作区版本 Hash 未接入，当前用修改证据指纹近似去重。执行前门禁依赖只读能力声明和可识别目标范围，无法识别的动作保守放行；暂缓不等于模型已真正重新规划。真实模型对照评测与阈值校准未完成。这些缺口必须在声称完全符合本契约前补齐或经证据修订契约。

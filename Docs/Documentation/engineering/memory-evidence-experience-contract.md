# RepoTerm 记忆保留、证据与经验边界合同

> 状态：已完成（2026-08-21）
>
> 适用范围：RepoTerm 持久化记忆轻量化重构的最后收尾阶段
>
> 上位合同：`memory-refactor-contract.md`
>
> 本文只补充并收紧“什么可以成为长期记忆、什么算验证证据、证据如何支撑经验、谁负责注入”这些边界。与上位合同冲突时，以本文对记忆写入和证据判定的更严格规则为准。
>
> 方案依据：本文不是独立臆造，设计取舍参考 `dsh-memory-evolve`、LangMem、Letta Code 与 Mem0 的公开实现；RepoTerm 只吸收适合本地 Coding Agent 的轻量部分。

## 1. 目标

记忆系统必须保留三类有长期价值的信息：

1. 用户明确要求长期保留的必要信息；
2. 支撑经验可信度的有界证据摘要与来源引用；
3. 经验证、可在后续任务复用的经验。

记忆系统不得把“Agent 见过的所有内容”自动转换成经验。源码内容、普通搜索结果、完整工具输出、完整对话、模型思考和一次性中间状态应留在 Session Transcript、Trace 或 Checkpoint 中，不进入长期记忆。

目标闭环：

```text
用户明确记忆 ──────────────────────────────→ active 必要信息

任务观察 → 执行 → 有效验证证据 → 经验提炼 → pending 候选
                                                ↓ 人工 approve
                                              active 经验

原始工具结果 / 对话 / Diff / 测试日志 ───────→ Transcript / Trace
                                                ↓ 只保留摘要和引用
                                           Memory Evidence Digest
```

## 2. 三个概念必须分开

| 对象 | 含义 | 保存位置 | 是否直接注入 Prompt |
|---|---|---|---|
| 必要信息 | 稳定偏好、约束、决策和明确命令 | SQLite `memory_entries` | active 时可以 |
| 证据 | 证明某条经验可信的验证结果 | 记忆中的有界摘要与 Session/Turn 引用；原文留在 Trace/Transcript | 不单独注入 |
| 经验 | 在明确上下文中可复用的“条件→行动→结果” | 先 pending，批准后 active | active 时可以 |

核心原则：**证据不是另一条可检索经验，而是经验候选的来源与可信依据。**

### 2.1 开源方案依据与取舍

| 参考实现 | 值得吸收的设计 | RepoTerm不照搬的部分 |
|---|---|---|
| [dsh-memory-evolve](https://github.com/csyangwen/dsh-memory-evolve/blob/main/docs/rules.md) | 高影响记忆先进入待确认队列；只注入低频、稳定内容；项目范围隔离；重复建议累计信号；归档内容不再注入但可恢复 | 不复制“五轨记忆”和每回合项目/每日日志；RepoTerm已有Session Transcript与Trace，重复记录只会制造两套真值 |
| [LangMem](https://github.com/langchain-ai/langmem) | 显式的create/update/delete生命周期；namespace隔离；区分回合内主动管理与后台提炼 | 不引入LangGraph、Embedding、后台LLM Memory Manager，不允许模型无审批直接改写高影响记忆 |
| [Letta Code](https://github.com/letta-ai/letta-code/blob/main/src/agent/prompts/letta.md) | 把完整消息历史与精炼核心记忆分开；核心记忆只保留能改变未来行为的稳定知识；可从历史重新查证；禁止密钥；记忆保持精简 | 不引入MemFS、Git记忆仓库、Recall子Agent和自动“睡眠反思” |
| [Mem0](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) | 写入前进行查重/冲突处理；支持更新、删除、过期与变更历史 | 不引入向量数据库、实体图谱、Embedding和对每段对话执行LLM事实抽取 |

因此RepoTerm采用的不是某个框架的完整实现，而是一条收敛后的主线：

```text
完整历史留在 Transcript / Trace（可追溯，但不自动注入）
                         ↓ 只在满足触发条件时提炼
必要信息或经验证经验 → pending候选 → 用户确认 → active精炼记忆
                                             ↓ 过期或失效
                                           archived
```

### 2.2 RepoTerm只保留三种长期记忆语义

| 逻辑语义 | 复用当前模型字段 | 保存内容 | 默认状态 |
|---|---|---|---|
| `user_preference` | `Kind.PREFERENCE` | 稳定的用户偏好、沟通方式和长期约束 | explicit→active；implicit→pending |
| `project_fact` | `Kind.DECISION`或`Kind.CONSTRAINT` | 当前项目长期有效的约定、架构决策、固定命令和非易失事实 | explicit→active；system→pending |
| `experience` | `Kind.LESSON + SourceType.VERIFIED_TASK` | 可复用的“适用条件→有效动作→验证结果” | pending |

这三项是业务语义，不要求新增`memory_kind`数据库列。当前`Kind`、`SourceType`和`Scope`已经足以表达，避免重复建模。

`archived`是生命周期状态，不是第四种记忆。完整对话、任务日志、工具输出和代码现状不是长期记忆类型，它们属于Session Transcript或Trace。

这条边界同时意味着：**RepoTerm不新增project/daily流水账记忆。** 已有会话持久化负责“发生过什么”，记忆系统只负责“以后还值得主动带入什么”。

## 3. 什么可以成为长期记忆

### 3.1 用户明确写入的必要信息

用户通过 `# remember`、`/memory add` 或等价显式入口写入时，可以直接成为 active，但仍必须通过敏感信息检查和作用域检查。

允许内容：

- 稳定的输出偏好，例如“回答使用中文”；
- 项目长期约束，例如“不得修改 tests 目录”；
- 已确认的架构决策，例如“数据库迁移统一使用 Alembic”；
- 稳定的项目命令，例如“单元测试命令为 `pytest -q`”；
- 用户明确要求跨会话保留的信息。

不建议但仍尊重用户授权的内容，可以写入 active；系统不得擅自把用户明确记忆降级为模型候选。

### 3.2 系统提炼的必要信息候选

用户没有明确说“记住”时，系统不得根据单句对话直接建立active记忆，只能在以下情况下提出pending候选：

- 同一稳定用户偏好在至少2个独立Turn或Session中出现；
- 用户纠正了现有记忆，形成明确冲突；
- 某项项目约定、架构决策或固定命令已被用户确认；
- 某项非易失项目事实同时具备来源引用，且不能通过随时重读仓库低成本获得。

相同scope和规范化key的候选只保留一条并累计`signal_count`，不得重复追加。隐式候选必须由用户approve后才可注入。

源码当前状态、依赖版本、测试数量等可以随仓库变化且能够重读得到的信息，默认不进入`project_fact`。

### 3.3 系统提炼的经验候选

系统只能在同时满足以下条件时创建经验候选：

1. 当前 Turn 有明确任务目标；
2. 当前 Turn 最终状态不是 blocked、max_steps、verification_failed 或异常终止；
3. 至少存在一条 `VALIDATION` 级证据；
4. 经验能够写成“适用条件→采取动作→验证结果”；
5. 经验预计对后续 Turn 有用，而不是当前任务的临时细节；
6. 不包含凭据、完整工具输出、长代码块或模型推测；
7. 与同 scope 已有内容不构成完全重复；
8. 每个 Turn 最多创建 1 条经验候选。

满足条件后也只能写成 pending，由用户通过 `/memory approve <id>` 激活。

系统经验提炼只在“成功完成或成功恢复且具有验证证据”的Turn末尾触发，不进行每回合全量记忆审查，也不为提炼额外调用一次LLM。这样可以避免把普通对话和观察流水转成经验，同时控制成本和非确定性。

### 3.4 明确禁止进入长期记忆

- `read_file`、`grep`、目录遍历等普通观察结果；
- 完整源码、完整Diff、完整测试日志和完整Shell输出；
- 模型的思考过程、规划草稿、猜测和未验证结论；
- 一次性临时路径、临时端口、随机ID和中间变量；
- 尚未定位根因的报错；
- 仅仅“工具调用成功”，但没有证明任务正确的结果；
- API Key、Token、Cookie、密码及疑似敏感内容；
- Session Transcript、WorkingMemory、StableTaskPack已有的信息副本；
- 只对当前消息有用、跨Turn没有复用价值的事实。

## 4. 证据等级与判定

### 4.1 证据等级

```text
OBSERVATION  观察证据：帮助理解任务，但不能证明完成
CHANGE       变更证据：证明发生了修改，但不能证明修改正确
VALIDATION   验证证据：能够支持任务或经验结论
CONFIRMATION 用户确认：用户明确确认事实或接受结果
```

只有 `VALIDATION` 或 `CONFIRMATION` 可以支持经验候选。

### 4.2 工具结果分类

| 场景 | 等级 | 能否支持经验候选 |
|---|---|---:|
| `read_file`、`grep_files`、`list_files`、符号检索 | OBSERVATION | 否 |
| `edit_file`成功、Diff已应用、Checkpoint已创建 | CHANGE | 否 |
| Pytest/测试工具退出码0 | VALIDATION/test | 是 |
| build命令退出码0 | VALIDATION/build | 是 |
| lint/type-check退出码0 | VALIDATION/static_check | 是 |
| JSON/YAML/配置Schema校验成功 | VALIDATION/schema | 是 |
| 文件内容断言、文件Hash或禁止路径断言通过 | VALIDATION/file_assert | 是 |
| 会话恢复后状态、Checkpoint和测试共同通过 | VALIDATION/recovery | 是 |
| 用户明确说“这个结论是对的，请记住” | CONFIRMATION/user | 是 |
| 任意普通Shell命令退出码0 | 默认OBSERVATION | 否 |
| 测试失败、超时、权限拒绝 | 失败证据 | 不能单独支持成功经验 |

`run_command`不能仅凭退出码0自动成为 VALIDATION。必须根据命令和参数识别其语义；无法识别时按 OBSERVATION 处理。

### 4.3 失败经验

失败本身可以成为经验素材，但不能在失败发生时直接记忆。只有满足以下条件才能形成“失败恢复经验”：

1. Trace 中存在原始失败证据；
2. 后续动作明确修复或绕开该失败；
3. 最终存在 VALIDATION/recovery、test、build 或等价成功证据；
4. 经验描述包含失败条件、有效处理动作和最终验证结果。

未定位根因、未恢复或只发生一次的失败不得成为长期经验。

## 5. 结构化证据对象

新增一个轻量、不可携带原始大输出的证据对象。名称可以调整，但语义不得弱化：

```python
@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    level: EvidenceLevel
    kind: EvidenceKind
    tool_name: str
    ok: bool
    summary: str
    fingerprint: str
    source_session_id: str | None
    source_turn_id: str | None
    created_at: float
```

约束：

- `summary`清洗空白后最多240字符；
- 不保存完整stdout/stderr、Diff或源码；
- `fingerprint`对规范化后的工具名、关键输入、退出状态和摘要计算SHA-256；
- `source_session_id`和`source_turn_id`用于回到Transcript/Trace查原始证据；
- 证据摘要再次执行敏感信息检测与脱敏；
- 每个经验候选最多保留3条证据摘要；
- Prompt默认只显示经验正文和证据类型，不显示证据详细摘要。

## 6. TurnVerificationState 必须成为唯一证据真值

当前“见过任意ToolResult就有证据”的逻辑必须取消。

目标状态：

```python
TurnVerificationState:
    evidence_ready: bool
    evidence_items: list[VerificationEvidence]
    evidence_summary: str
    last_verification_note: str
```

实施规则：

1. `record_tool_result()`只记录工具观察、成功数和错误数，不再自动设置`evidence_ready=True`；
2. 工具执行完成后，由独立的`classify_verification_evidence(tool_name, input, result)`返回证据或None；
3. 只有分类结果为VALIDATION/CONFIRMATION且`ok=True`时，才调用`record_verification_evidence()`；
4. `has_verification_evidence()`只检查有效`evidence_items`；
5. Verification Guard、Trace、Reflection和MemoryService都读取同一个`TurnVerificationState`，禁止各自重新推断；
6. Reflection不得再根据任意`tool_result`字典自行判断验证成功。

## 7. MemoryService 写入硬约束

### 7.1 显式用户记忆

```python
remember_explicit(...)
```

- source_type固定为`explicit_user`；
- 通过检查后可以直接active；
- 不要求VerificationEvidence。

### 7.2 系统经验候选

```python
propose_experience(..., evidence: Sequence[VerificationEvidence])
```

- source_type固定为`verified_task`；
- evidence为空或没有VALIDATION/CONFIRMATION时必须拒绝；
- 状态固定为pending；
- 必须记录source_session_id和source_turn_id；
- 调用者不能通过`verified=True`布尔值绕过证据校验；
- approve后证据摘要和来源引用保持不变。

现有`propose(..., verified=True)`中的布尔参数应删除或废弃，因为它无法证明调用者真的持有验证证据。

### 7.3 更新语义

- 每次候选写入必须先在同scope、同业务语义映射和规范化key下决策为`CREATE`、`UPDATE`、`ARCHIVE`、`DELETE`或`NOOP`，禁止blind append；
- 完全重复时执行`NOOP`并更新命中/信号计数，不创建新行；
- 新内容使旧内容过时时执行`UPDATE`，并保留版本历史；
- 内容冲突但无法自动判断真值时，保留旧active记录，新内容进入pending冲突候选；
- 更新创建新版本，旧版本变为superseded；
- 用户修改经验正文后，旧证据不能自动证明新正文；
- 用户显式更新可把新版本标记为`explicit_user`，或清空旧证据并要求重新验证；
- 不得在修改正文后原样继承与新内容不匹配的证据；
- archive可恢复，purge只用于用户明确确认或敏感数据清理。

### 7.4 触发边界

记忆写入逻辑只允许由三类事件触发：

1. 用户显式调用记忆命令或明确表达“记住/更新/删除”；
2. Turn成功结束且存在VALIDATION/CONFIRMATION证据，系统尝试生成最多1条经验候选；
3. 新信息与已注入记忆发生明确冲突，系统生成更新/归档候选。

普通消息到达、普通工具成功、文件被读取、上下文被压缩、Session被保存，都不得触发长期记忆写入。

## 8. 经验正文格式

第一版不引入LLM Curator、Embedding、向量数据库或第二次模型调用。经验使用可审计模板生成：

```text
适用条件：<什么时候适用>
有效动作：<采取什么动作>
验证结果：<通过什么类型的证据确认>
```

约束：

- 正文建议40至600字符；
- 不包含完整日志和大段代码；
- 不写“本次任务已完成”这类不可复用描述；
- 不写无法从Trace和证据支持的根因；
- 相同scope和content_hash不重复写入；
- 同scope、同key冲突进入pending，不静默覆盖。

## 9. 检索和注入边界

- 只检索active、未过期、当前scope可见的记录；
- pending、archived、superseded、rejected不参与Prompt注入；
- 排序保持确定性，不使用LLM Reranker或Embedding；
- 默认最多5条、总预算800 Token；
- 证据摘要用于审计和人工批准，不参与正文相关性检索；
- Prompt中的记忆必须标明advisory，不能覆盖系统、权限和工具安全规则；
- 同一Turn最多存在一个`## Persistent Memory`块。

## 10. 单一数据库与组件所有权

### 10.1 SQLite唯一真值源

生产环境只使用：

```text
~/.repoterm/memory.sqlite3
```

规则：

- Main创建一个MemoryService；
- TUI、CLI、Headless、Agent Loop和Reflection共享该实例；
- project/branch隔离通过project_key和branch_name实现；
- 生产路径不得回退到`.repoterm-memory-runtime/memory.sqlite3`；
- 测试可以显式传入tmp_path数据库；
- 任何组件缺少MemoryService时应返回可观察错误或禁用记忆，不得悄悄打开第二个数据库。

### 10.2 Prompt唯一注入点

- Agent Loop是唯一Prompt注入所有者；
- TUI只更新基础system prompt，不调用`get_relevant_context()`；
- Main和Headless只负责创建并传递MemoryService；
- CLI只做生命周期操作，不参与Prompt拼接；
- ContextCompactor不得重新检索并追加第二个记忆块；
- 压缩时可以保留现有块，或先移除旧块再通过统一Injector重建，但最终只能有一个块。

## 11. `/user`与`Kind.PREFERENCE`

- `/user`不再维护第二套持久化真值；
- `/user`作为global scope下`Kind.PREFERENCE`记忆的兼容适配入口；
- `USER.md`只作为迁移输入或只读兼容展示；
- 禁止SQLite和USER.md双写；
- 用户明确修改偏好时走`remember_explicit`或版本更新流程。

## 12. 敏感旧数据迁移

采用严格策略：

1. 一个来源中发现任意敏感记录，停止该来源的本次迁移；
2. 该来源不插入任何新记录；
3. 不写migration marker；
4. 报告来源路径、敏感记录数量和处理建议，但不得输出内容；
5. 其他独立来源可以继续迁移；
6. 用户清理来源文件后，因没有marker可以重新执行。

这条规则只针对旧文件迁移；用户显式写入敏感内容仍应直接拒绝。

## 13. 旧实现清理边界

新链路验收通过后：

- 将仍需要的通用控制器从旧`memory_injector.py`迁移到职责明确的控制模块，或确认无调用后删除；
- 删除旧`memory.py`、`memory_pipeline.py`、`memory_reranker.py`、`vector_memory.py`、`memory_curator_agent.py`和`timeline_memory.py`；
- 删除或改写仅绑定旧内部结构的测试；
- 保留表达用户可观察行为的契约测试；
- `working_memory.py`、StableTaskPack、Session Transcript和Trace仍不属于持久化记忆，不得借机删除或合并；
- 短期兼容别名只能委托新服务，不能恢复第二套存储或检索逻辑。

## 14. 允许修改的生产文件

实现Agent只允许修改以下生产接缝：

- `repoterm/turn_kernel.py`
- `repoterm/agent_loop.py`
- `repoterm/agent_reflection.py`
- `repoterm/memory/__init__.py`
- `repoterm/memory/models.py`
- `repoterm/memory/store.py`
- `repoterm/memory/service.py`
- `repoterm/memory/injector.py`
- `repoterm/memory/legacy.py`
- `repoterm/tui/input_handler.py`
- `repoterm/context_compactor.py`
- `repoterm/cli_commands.py`
- `repoterm/main.py`
- `repoterm/headless.py`
- `repoterm/cybernetic_orchestrator.py`
- `repoterm/cybernetic_ablation.py`
- `repoterm/user_profile.py`，仅用于收敛`/user`适配层
- 本文第13节明确列出的旧模块删除

如果需要修改其他生产文件，必须先报告原因并等待批准。不得顺手重构Tool Runtime、Session、权限、TUI渲染或Agent状态机的其他逻辑。

## 15. 实施阶段

### 阶段G1：证据模型与服务硬约束

1. 增加EvidenceLevel、EvidenceKind和VerificationEvidence；
2. 扩展TurnVerificationState；
3. 将观察结果与验证证据分离；
4. 复用现有`Kind`、`SourceType`、`Scope`、版本和归档字段，只新增缺失的`signal_count`与证据字段；
5. MemoryService要求结构化证据，不接受`verified=True`作为证明；
6. 写入统一执行CREATE/UPDATE/ARCHIVE/DELETE/NOOP判定；
7. 增加SQLite schema迁移，旧数据库必须可升级。

### 阶段G2：Runtime与Reflection接线

1. 实现工具结果证据分类器；
2. Verification Guard、Trace和Reflection共享TurnVerificationState；
3. 每Turn最多提议一条经验；
4. 只在显式记忆、验证成功或明确冲突三类事件上触发候选逻辑；
5. 未验证、失败未恢复和只读检索任务不生成经验；
6. 不实现每回合全量审查，不新增project/daily流水账。

### 阶段G3：单库、单点注入与压缩

1. TUI生命周期命令接收现有MemoryService；
2. 删除TUI预注入；
3. ContextCompactor不再追加第二个记忆块；
4. 禁止生产路径创建项目级fallback数据库；
5. 增加TUI和压缩后的单块断言。

### 阶段G4：兼容收敛与旧模块删除

1. `/user`转发到global scope下的`Kind.PREFERENCE`；
2. 迁移敏感来源采用整源回滚；
3. 移出或删除旧MemoryInjectionController；
4. 删除旧Pipeline、Reranker、Vector、Curator和Timeline；
5. 更新文档和测试路径。

### 阶段G5：完整验收

每阶段单独提交。任一阶段导致L0或L1失败时停止，不得继续下一阶段。

## 16. 必须新增的行为测试

至少覆盖：

1. `read_file`成功不产生VerificationEvidence；
2. 普通`run_command`成功不自动成为VALIDATION；
3. Pytest成功产生VALIDATION/test；
4. 测试失败不产生成功经验；
5. 失败后修复并通过测试，只生成一条pending恢复经验；
6. 没有结构化证据时MemoryService拒绝系统经验候选；
7. 用户显式记忆不要求验证证据并可直接active；
8. 经验正文不包含完整工具输出；
9. 证据摘要长度受限并执行敏感信息过滤；
10. approve后经验可注入，pending不得注入；
11. 用户修改经验正文后旧证据不会被错误继承；
12. 同一Turn最多生成一条候选；
13. TUI的`/memory pending`与Runtime看到同一个数据库；
14. Main、Headless、TUI、CLI使用同一db_path；
15. TUI进入Agent Loop前没有记忆块；
16. 普通Turn最终只有一个Persistent Memory块；
17. Session Memory压缩后仍只有一个Persistent Memory块；
18. 敏感迁移来源整源回滚且不写marker；
19. 五类旧数据来源迁移均有自动化夹具；
20. 旧记忆模块不存在生产import；
21. 单次隐式偏好信号不会成为active记忆，达到2次独立信号后只生成一个pending候选；
22. 重复候选执行NOOP并累计`signal_count`，不会blind append；
23. 与active记忆冲突的新内容进入pending更新候选，不静默覆盖；
24. Session保存、上下文压缩和普通消息不会触发长期记忆写入；
25. 生产数据库中不存在project/daily流水账记忆类型。

## 17. 回归门禁

实现完成后必须报告：

```powershell
python -B -m pytest -q --tb=short -p no:cacheprovider tests/memory
python -B -m pytest -q --tb=short -p no:cacheprovider tests/contracts
python -B -m pytest -q --tb=short -p no:cacheprovider tests/test_agentops_scenarios.py tests/test_compaction_robustness.py tests/test_agentops_proof_artifacts.py::test_memory_lifecycle_trace_updates_deletes_and_preserves_unrelated_memory tests/test_memory_e2e.py::TestCrossSessionMemoryContinuity::test_memory_survives_session_close_and_reopen
python benchmarks/runtime_regression_eval.py --rounds 3
```

要求：

- 新记忆测试全部通过；
- L0全部通过；
- L1连续3轮全部通过；
- L2为60/60，不调用真实Provider；
- `git diff --check`通过；
- 工作区无无关改动；
- 旧模块静态搜索无生产import；
- 运行时只存在一个SQLite真值源和一个Prompt记忆块。

## 18. 实现Agent交付物

实现Agent必须提交：

1. 每个阶段的独立本地commit；
2. 修改和删除文件列表；
3. Evidence分类表与实际代码映射；
4. SQLite schema版本和升级路径；
5. 一条显式用户记忆Trace；
6. 一条“只读观察不生成经验”Trace；
7. 一条“失败→修复→验证→pending经验”Trace；
8. TUI与Runtime共享数据库的测试证据；
9. 压缩前后单个记忆块的测试证据；
10. L0、L1三轮、L2和静态边界结果；
11. 未完成项和已知限制；
12. 未经主审批准不得推送远端。

## 19. 最终验收判断

只有同时满足以下条件，才能宣称“记忆系统保留必要信息、证据与经验，而不是保存所有见过的内容”：

- 用户明确内容与系统经验走不同写入路径；
- 普通观察不会自动成为验证证据；
- 系统经验有结构化验证证据且只进入pending；
- 原始证据留在Trace/Transcript，长期记忆只保留有界摘要和引用；
- active记录才可注入；
- 单库、单服务、单注入点成立；
- 旧复杂记忆链路已经退出并删除；
- 动态测试与静态边界全部通过。

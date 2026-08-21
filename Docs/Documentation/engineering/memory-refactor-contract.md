# RepoTerm 持久化记忆轻量化重构合同

> 状态：已完成（2026-08-21）
> 基线分支：`refactor/v2`  
> 重构前标签：`v0.1-before-refactor`  
> 保护线说明：`tests/contracts/README.md`  
> 本文用途：作为实现 Agent 的唯一任务边界、实施顺序和验收依据。

## 1. 任务目标

将 RepoTerm 当前分散、自动化程度过高的持久化记忆系统，重构为一个可解释、可审计、可迁移的轻量子系统。

重构后的系统必须完成以下闭环：

```text
显式用户记忆 / 系统候选记忆
          ↓
     pending 或 active
          ↓
作用域过滤 → 相关性排序 → Token 预算筛选
          ↓
      单次注入 Prompt
          ↓
更新 / 归档 / 恢复 / 彻底删除
```

目标不是增加记忆功能数量，而是减少实现路径，同时保留以下产品能力：

- 跨会话持久化；
- global、project、branch 三种作用域；
- 显式写入、候选确认、检索注入；
- 更新、归档、恢复、过期和彻底删除；
- 来源追踪、版本关系和错误记忆纠正；
- 旧数据的非破坏性迁移。

## 2. 为什么要做

当前持久化记忆相关实现分散在多个模块中：

| 文件 | 当前规模 | 主要问题 | 本次处置 |
|---|---:|---|---|
| `repoterm/memory.py` | 约 1737 行 | 数据模型、文件存储、检索、分类、压缩、冲突、生命周期混在一起 | 由新记忆包替换 |
| `repoterm/memory_injector.py` | 约 404 行 | 注入控制、缓存、相关性计算和格式化重复 | 合并到轻量 Injector |
| `repoterm/memory_pipeline.py` | 约 481 行 | 同时编排检索、向量、重排、写入和维护 | 删除 |
| `repoterm/memory_reranker.py` | 约 289 行 | 为记忆检索再次调用 LLM，增加成本和非确定性 | 删除 |
| `repoterm/vector_memory.py` | 约 190 行 | 当前记忆规模不需要双检索系统 | 删除 |
| `repoterm/memory_curator_agent.py` | 约 367 行 | 独立 Curator 自动合并和生成记忆，污染边界较大 | 删除 |
| `repoterm/timeline_memory.py` | 约 3295 行 | 大量领域化时间推理，当前无生产调用者 | 确认无调用后删除 |
| `repoterm/user_profile.py` | 约 471 行 | 与 global 偏好记忆职责重叠 | 收敛为兼容适配层后删除或显著缩减 |
| `repoterm/working_memory.py` | 约 217 行 | 名称包含 memory，但职责是单轮上下文保护 | **不属于本次范围，禁止修改** |

当前 `agent_loop.py`还同时创建 MemoryManager、LLM Reranker、MemoryInjector和MemoryPipeline，并存在启动时注入与Turn内注入并存的风险。重构必须把记忆收敛为一个服务入口和一个Prompt注入点。

## 3. 工程边界

### 3.1 本次范围内

- 持久化记忆的数据模型、存储、生命周期、检索和Prompt注入；
- 当前记忆JSON/Markdown和用户偏好文件的兼容迁移；
- `/memory`、`# <memory>`以及必要的旧命令兼容；
- Runtime、CLI、Headless和控制器中的记忆接入代码；
- 持久化记忆相关测试、文档和迁移报告；
- 无生产调用者的持久化记忆死代码清理。

### 3.2 明确不在范围内

以下模块不得借本任务顺手重构：

- Agent Turn的 `explore → execute → verify`状态机；
- Verification Guard、最大步数、空响应和暂停恢复；
- ContextManager、MicroCompactor、StableTaskPack及Token压力策略；
- Session Snapshot、Delta、Checkpoint、Replay、Resume和Rewind；
- PermissionManager、Diff审批和文件写入链路；
- ToolDefinition、ToolRegistry、ToolResult协议；
- Model Adapter、Provider fallback和API retry；
- TUI渲染、输入解析和交互布局；
- AgentOps任务集、Grader规则和报告口径；
- Cybernetic控制体系中与记忆无关的控制器。

### 3.3 允许跨边界修改的文件

这些文件只能修改记忆接缝，不得做附带重构：

| 文件 | 允许修改内容 |
|---|---|
| `repoterm/main.py` | 构造新MemoryService；移除重复注入；保留用户命令入口 |
| `repoterm/headless.py` | 注入同一个MemoryService，不再自行拼接另一份记忆 |
| `repoterm/agent_loop.py` | 接收记忆端口；每个Turn只调用一次Injector；删除旧Pipeline/Reranker接线 |
| `repoterm/cli_commands.py` | 将 `/memory`、`/user`转发到新服务或兼容适配器 |
| `repoterm/agent_reflection.py` | 自动反思只能生成pending候选，不得直接写active记忆 |
| `repoterm/cybernetic_orchestrator.py` | 删除旧MemoryPipeline依赖或改为薄委托；其他控制逻辑不动 |
| `repoterm/prompt.py` | 仅在确有重复注入时移除旧memory_context接缝 |
| `repoterm/config.py` | 增加数据库路径、注入条数和Token预算配置；不得改Provider配置 |

如果实现需要修改上述表格之外的生产文件，必须停止实现并提交“为什么现有边界无法完成”的说明，等待批准。

### 3.4 禁止事项

- 不引入LangChain、向量数据库、Embedding模型或新的第三方依赖；
- 不调用真实LLM进行记忆重排、总结或维护；
- 不在每个Turn结束后自动写入任务过程；
- 不把Session Transcript复制成记忆；
- 不保存原始工具大输出、完整Diff、临时报错或一次性任务叙述；
- 不自动删除旧记忆文件或用户数据；
- 不通过修改测试断言来掩盖行为退化；
- 不修改既有60/60 Runtime评测的任务数或Grader口径；
- 不推送远端、不删除重构前Tag、不清理保存演示残留的stash。

## 4. 目标架构

目标目录：

```text
repoterm/memory/
├── __init__.py       # 唯一公开入口和短期兼容导出
├── models.py         # MemoryEntry、Scope、Status、Kind
├── store.py          # SQLite schema、事务和基础CRUD
├── service.py        # 生命周期、去重、版本更新、作用域检索
├── injector.py       # 排序、Token预算、Prompt格式化
└── legacy.py         # 一次性旧数据迁移；稳定后可移除
```

依赖方向必须保持：

```text
Main / Headless / CLI
          ↓
     MemoryService
       ↙       ↘
MemoryStore   MemoryInjector
          ↓
   Python标准库 sqlite3
```

约束：

- `store.py`不知道Agent、Prompt、TUI和Tool；
- `service.py`不知道Model Adapter和Session内部格式；
- `injector.py`只消费Service返回的active记忆；
- 数据库路径和Token估算器由组合根注入，核心模块不得依赖全局单例；
- Runtime只依赖公开服务/协议，不访问数据库和内部字段；
- 记忆包不得反向导入 `agent_loop.py`、`main.py`或TUI。

## 5. 数据模型

建议的最小实体：

```text
MemoryEntry
├── id                 UUID
├── scope              global | project | branch
├── kind               preference | decision | constraint | lesson
├── key                可选的稳定语义键，例如 test_command
├── content            记忆正文
├── status             pending | active | archived | superseded | rejected
├── project_key        project/branch必填
├── branch_name        branch必填
├── source_type        explicit_user | verified_task | migration
├── source_session_id  可选
├── source_turn_id     可选
├── version            从1递增
├── supersedes_id      被当前版本替代的记忆ID
├── content_hash       规范化正文哈希
├── created_at
├── updated_at
└── expires_at         可选
```

### 5.1 作用域规则

- `global`：跨仓库用户偏好和通用约束；不带project_key和branch_name；
- `project`：仅当前工作区有效；必须带project_key；
- `branch`：仅当前工作区的当前Git分支有效；必须同时带project_key和branch_name；
- project_key第一版使用规范化绝对工作区路径的稳定哈希；不要在本次加入远端URL识别；
- 非Git目录不能创建branch记忆，应回退为project或返回明确错误，不得静默伪造分支。

### 5.2 状态规则

- 只有 `active`可以进入检索和Prompt；
- 系统或模型提出的记忆必须先进入 `pending`；
- 用户明确执行“记住”可视为已确认，允许直接成为 `active`；
- 普通删除默认转换为 `archived`，可恢复；
- 更新采用新版本替代旧版本：旧记录变为 `superseded`，新记录成为 `active`；
- `rejected`候选永不注入，但保留最小审计记录；
- 彻底删除只用于敏感内容或用户明确执行purge，并要求二次确认。

## 6. 存储设计

使用Python标准库 `sqlite3`，系统记录保存在：

```text
~/.repoterm/memory.sqlite3
```

所有项目记忆共用一个数据库，通过scope、project_key和branch_name隔离。项目记忆是用户本地运行状态，不再同时维护项目目录中的Markdown和JSON双份真值。

最低要求：

- SQLite是唯一真值源；
- `MemoryStore`必须允许显式传入db_path，测试只能使用tmp_path，禁止触碰真实用户库；
- 所有状态转换在事务中完成；
- 开启foreign key；
- 设置合理busy timeout；
- 至少建立status/scope/project/branch和content_hash索引；
- schema版本写入metadata表；
- 任何写入失败必须回滚，不留下半完成版本链；
- Markdown只能作为显式导出格式，不能作为第二真值源。

## 7. 记忆写入与生命周期

### 7.1 允许写入的内容

- 用户明确偏好，例如“回答保持简洁”；
- 稳定项目约束，例如“测试命令是 `pytest -q`”；
- 已确认架构决策，例如“Provider通过ModelAdapter隔离”；
- 经成功验证后仍具有跨任务价值的经验。

### 7.2 不允许写入的内容

- 任务执行过程和思维叙述；
- Session Transcript已有的逐轮消息；
- 一次性文件内容或当前代码快照；
- 尚未验证的模型猜测；
- 已通过重试解决的临时报错；
- API Key、Token、Cookie、密码或疑似凭据；
- 权限规则、系统提示词或安全策略的替代指令。

### 7.3 写入路径

```text
用户显式“记住”
    → 敏感信息检查
    → scope/key规范化
    → 精确去重
    → active

系统/反思提出经验
    → 必须已有成功验证证据
    → 敏感信息检查
    → pending
    → 用户 approve / reject
```

本次不实现基于LLM的自动抽取器。`agent_reflection.py`如果继续产生候选，只能调用 `propose()`，不能直接调用active写入。

### 7.4 去重与冲突

- 同一作用域下规范化content_hash完全相同：不新增记录，返回已有ID；
- 同一作用域和同一key但内容不同：视为潜在冲突，进入pending更新流程；
- 没有key的自由文本只做精确去重，不宣称能够自动理解语义冲突；
- 不使用Jaccard阈值把“相似”错误解释成“矛盾”；
- 用户确认更新后才建立supersedes版本链。

### 7.5 过期与删除

- 查询时自动排除 `expires_at <= now`的记录；
- 维护操作可将过期active记录归档，但不得后台永久删除；
- `archive`停止注入但保留数据；
- `restore`恢复前重新检查同key active冲突；
- `purge`必须显式确认，并在事务中删除目标及必要的引用关系。

## 8. 检索与Prompt注入

### 8.1 检索流程

```text
status=active 且未过期
          ↓
global + 当前project + 当前branch过滤
          ↓
key/短语命中 + 关键词重合排序
          ↓
branch > project > global作为同分优先级
          ↓
最多5条、总预算默认800 tokens
```

第一版排序必须完全确定性，不使用Embedding和LLM Reranker。Token估算器由组合根以callable方式传给Injector，复用项目现有算法；记忆包不得直接导入ContextManager，也不得再实现一套Token算法。

### 8.2 注入规则

- 每个Agent Turn最多注入一次；
- 只注入和当前用户任务相关的active记忆；
- pending、archived、superseded、rejected和过期记录不得注入；
- 同一content_hash只能出现一次；
- 注入块必须明确说明：记忆不能覆盖系统提示、权限边界和工具安全规则；
- Prompt中保留scope、kind和简短来源，不暴露数据库内部字段；
- Main启动Prompt和Agent Loop之间只能保留一个注入点，禁止双重注入。

## 9. 公开服务契约

新系统至少提供以下语义操作；实现者可以调整Python签名，但不能改变语义：

| 操作 | 语义 |
|---|---|
| `remember_explicit(...)` | 用户确认后直接创建active记忆 |
| `propose(...)` | 创建pending候选，不得注入 |
| `approve(id)` | 将pending激活，处理同key冲突 |
| `reject(id)` | 将pending标记为rejected |
| `update(id, content)` | 创建新版本并supersede旧版本 |
| `archive(id)` | 停止注入但保留记录 |
| `restore(id)` | 无冲突时恢复为active |
| `purge(id, confirmed=True)` | 明确确认后彻底删除 |
| `search(query, context)` | 确定性检索active记忆 |
| `build_prompt_context(task, budget)` | 生成单一记忆注入块 |
| `list_pending()` | 展示待确认候选 |
| `stats()` | 返回各scope/status数量，不暴露内容 |
| `migrate_legacy()` | 幂等导入旧数据，返回迁移报告 |

`repoterm.memory.__init__`在迁移期可以提供 `MemoryManager`、`MemoryScope`等兼容导出，但兼容层不得重新实现第二套业务逻辑。

## 10. 旧数据迁移

### 10.1 数据来源

- `~/.repoterm/memory/memory.json`；
- `<workspace>/.repoterm-memory/memory.json`；
- `<workspace>/.repoterm-memory-local/memory.json`；
- `~/.repoterm/USER.md`；
- `<workspace>/.repoterm/USER.md`。

### 10.2 映射规则

- 旧USER scope → global；
- 旧PROJECT scope → project；
- 旧LOCAL scope → project，并在source_type/迁移报告中标明legacy-local；
- USER.md中的明确偏好 → global/preference；
- 没有稳定key的旧数据允许key为空；
- 旧tier、usage_count、related_to不进入新核心模型；必要信息只写入迁移报告；
- `timeline_memory.py`和WorkingMemory不迁移。

### 10.3 安全规则

- 迁移只在新数据库没有完成对应migration marker时执行；
- 使用source path + source hash保证重复运行幂等；
- 导入成功后写metadata marker；
- 无效记录跳过并报告，不得使整个数据库不可用；
- 旧文件保持原样，禁止自动改名、覆盖或删除；
- 迁移失败时新事务回滚，旧系统数据仍可读取；
- 提供导入数量、去重数量、跳过数量和错误列表。

## 11. 文件修改清单

### 11.1 新增

- `repoterm/memory/__init__.py`
- `repoterm/memory/models.py`
- `repoterm/memory/store.py`
- `repoterm/memory/service.py`
- `repoterm/memory/injector.py`
- `repoterm/memory/legacy.py`
- `tests/memory/`下的新行为测试

### 11.2 最小修改

- `repoterm/main.py`
- `repoterm/headless.py`
- `repoterm/agent_loop.py`
- `repoterm/cli_commands.py`
- `repoterm/agent_reflection.py`
- `repoterm/cybernetic_orchestrator.py`
- 必要时 `repoterm/prompt.py`
- 必要时 `repoterm/config.py`
- `tests/contracts/test_runtime_contract.py`：新增第6个持久化记忆生命周期契约
- `tests/contracts/README.md`：更新L0数量与命令结果
- README和记忆文档中的实现路径说明

### 11.3 删除候选

必须在新实现接线完成、`rg`确认无生产调用、测试通过后才删除：

- `repoterm/memory.py`（由同名package替换）
- `repoterm/memory_injector.py`
- `repoterm/memory_pipeline.py`
- `repoterm/memory_reranker.py`
- `repoterm/vector_memory.py`
- `repoterm/memory_curator_agent.py`
- `repoterm/timeline_memory.py`
- 与上述已删除实现强绑定、且不再表达产品行为的测试

`repoterm/working_memory.py`本次不得删除、移动或改名。

## 12. 实施顺序

每一阶段单独提交，阶段失败时不得继续下一阶段。

### 阶段A：新核心与单元测试

1. 建立memory package；
2. 实现models、SQLite store和service；
3. 实现生命周期、作用域过滤和确定性检索；
4. 使用临时目录数据库完成单元测试；
5. 此阶段不接入Agent Loop。

### 阶段B：迁移与兼容门面

1. 实现legacy幂等导入；
2. 用测试夹具覆盖USER/PROJECT/LOCAL和USER.md；
3. 提供旧MemoryManager调用的薄兼容层；
4. 禁止删除真实旧文件。

### 阶段C：单点Prompt接入

1. Main/Headless在组合根创建同一个MemoryService；
2. Agent Loop通过公开端口调用Injector；
3. 移除LLM Reranker、Pipeline和重复memory_context注入；
4. Reflection只写pending候选；
5. 验证每个Turn只有一个记忆注入块。

### 阶段D：命令与生命周期操作

1. 保留 `# <memory>`显式记忆入口；
2. `/memory`至少支持status/list/pending/approve/reject/update/archive/restore/delete；
3. `user:`兼容别名映射到global；
4. `local:`兼容别名映射到project并返回弃用提示；
5. 增加 `branch:`显式作用域；
6. `/user`通过适配器读取/写入global preference，或保留明确的兼容提示。

### 阶段E：删除旧实现

1. 使用 `rg`确认旧模块没有生产import；
2. 删除旧Pipeline、Reranker、Vector、Curator和Timeline；
3. 删除或改写仅绑定旧内部结构的测试；
4. 不得删除表达用户可观察行为的测试；
5. 更新README代码路径和架构说明。

### 阶段F：完整验收

执行第13节全部验收；输出变更清单、迁移报告、测试结果和已知限制。未经主审批准不得推送远端。

## 13. 验收标准

### 13.1 记忆行为测试

必须自动化覆盖：

1. pending候选不会被检索或注入；
2. approve后可以检索并注入；
3. global/project/branch作用域隔离正确；
4. branch、project、global同分时优先级正确；
5. 同scope、同content_hash不会重复写入；
6. 同scope、同key冲突不会自动覆盖；
7. update创建新版本，旧版本变为superseded；
8. archive停止注入，restore可恢复；
9. 过期记忆不会注入；
10. purge必须确认且只删除目标；
11. 重启Service后数据仍然一致；
12. 迁移重复执行不重复导入；
13. 无效旧记录被报告且不破坏有效记录；
14. 检索最多5条且不超过Token预算；
15. 同一Turn只出现一个记忆注入块；
16. 记忆不能覆盖权限和系统安全提示；
17. 疑似凭据不会进入active存储；
18. 非Git目录的branch请求有明确结果。

### 13.2 重构保护线

在已激活 `agent-env`的前提下执行：

```powershell
python -B -m pytest -q --tb=short -p no:cacheprovider tests/contracts
```

要求：新增记忆契约后全部通过，预期至少 `6/6`。

```powershell
python -B -m pytest -q --tb=short -p no:cacheprovider tests/test_agentops_scenarios.py tests/test_compaction_robustness.py tests/test_agentops_proof_artifacts.py::test_memory_lifecycle_trace_updates_deletes_and_preserves_unrelated_memory tests/test_memory_e2e.py::TestCrossSessionMemoryContinuity::test_memory_survives_session_close_and_reopen
```

要求：如果旧测试因公开API变更需要适配，只允许修改调用方式，不得弱化写入、更新、删除、隔离、持久化和注入断言；连续3轮全部通过。

```powershell
python benchmarks/runtime_regression_eval.py --rounds 3
```

要求：`60/60 passed`，不得调用真实Provider。

真实模型L3不作为每次实现门禁；仅在主审确认Runtime重构稳定后单独执行。

### 13.3 静态边界检查

必须报告以下命令结果：

```powershell
rg -n "memory_pipeline|memory_reranker|vector_memory|memory_curator_agent|timeline_memory" repoterm tests benchmarks
rg -n "from repoterm\.memory|import repoterm\.memory" repoterm
git diff --check
git status --short
```

验收条件：

- 已删除模块不存在生产import；
- 新memory package不存在反向依赖；
- 工作区没有与本任务无关的改动；
- 没有新增第三方依赖；
- 没有修改AgentOps任务数量和Grader。

### 13.4 轻量化检查

以下是代码审查信号，不以机械行数代替设计判断：

- 永久保留的持久化记忆生产模块不超过5个核心职责文件；
- 任一文件明显超过500行时必须解释为什么无法继续按职责拆分；
- 不存在第二套向量/LLM检索路径；
- 不存在后台自动Curator；
- 不存在JSON与Markdown双真值；
- Agent Loop不访问MemoryEntry内部字段或数据库；
- 记忆失败不得使Agent Turn崩溃，但必须产生可观察日志，禁止静默 `except Exception: pass`。

## 14. 回滚与故障处理

- 任一阶段导致L0或L1失败，停止并回退该阶段提交；
- SQLite迁移失败时继续保留旧数据，不自动切换真值源；
- 接入失败时允许临时通过兼容门面读取旧MemoryManager，但不得同时双写；
- 禁止“新旧系统双写一段时间”，避免状态分叉；
- 重构前状态可由 `v0.1-before-refactor`定位；
- 不得使用 `git reset --hard`或删除用户stash；
- 发现真实用户记忆存在敏感信息时停止迁移并报告，不得将内容打印到测试日志。

## 15. 实现Agent交付物

实现Agent完成后必须提交：

1. 修改文件与删除文件列表；
2. 新架构的数据流和依赖方向；
3. SQLite schema及版本号；
4. 旧数据迁移报告；
5. L0、L1三轮和L2结果；
6. `rg`边界检查结果；
7. 重构前后持久化记忆模块数量和代码规模；
8. 保留的兼容接口及后续可删除条件；
9. 未完成项和已知限制；
10. 每个阶段对应的本地commit，不得直接推送远端。

## 16. 主审验收问题

主审将重点检查：

- 为什么只有active记忆能注入？
- 显式用户记忆与系统候选记忆为什么走不同写入路径？
- 同key冲突如何避免错误覆盖？
- 更新为什么建立版本链，而不是原地覆盖？
- archive、superseded和purge的语义有什么区别？
- project_key和branch_name如何防止跨仓库污染？
- 旧LOCAL记忆为什么迁移为project，而不是擅自绑定当前分支？
- 为什么第一版不使用向量检索和LLM Reranker？
- 如何证明每个Turn只注入一次？
- 迁移失败时如何保证旧记忆仍然可用？
- 哪些记忆可以恢复，哪些敏感数据必须彻底删除？
- 为什么WorkingMemory、StableTaskPack和Session Transcript不属于本次重构？

只有当实现能够用代码、测试和迁移报告回答上述问题，才视为记忆轻量化完成。

# RepoTerm 记忆系统实现说明

本文说明当前轻量化记忆系统的实现方式、写入边界、运行时接线和验收方法。它描述的是代码中的实际行为，不是旧 `memory.py`、Pipeline 或向量检索实现的设计文档。

## 1. 总体目标

记忆系统只保存两类可以跨回合复用的内容：

1. 用户明确要求保留的偏好、约束或项目决策。这类内容可以直接进入 `active`。
2. 经过验证、可复用的经验。这类内容必须先进入 `pending`，用户批准后才会进入 `active`。

普通对话、源码正文、搜索结果、完整工具输出、Diff、模型推测和一次性任务过程留在 Session Transcript/Trace 中，不会因为“看过”或“工具返回成功”就自动变成长久记忆。

核心数据流如下：

```text
用户明确记忆 ───────────────────────────────→ active

工具观察 → 执行变更 → 语义化验证 → 经验候选 ─→ pending ─→ approve ─→ active
                         │
                         └─ 只保留有界证据摘要和 Session/Turn 引用
```

## 2. 代码结构

持久化记忆的核心职责收敛在 `repoterm/memory/`：

| 文件 | 职责 |
| --- | --- |
| `models.py` | `MemoryEntry`、scope/status/kind/source 枚举、`VerificationEvidence` 和敏感信息检查 |
| `store.py` | SQLite schema、连接生命周期、事务、查询和版本升级 |
| `service.py` | 记忆生命周期、作用域、查重、冲突、经验门禁和迁移事务 |
| `injector.py` | 确定性检索、最多 5 条记忆、Token 预算和单块 Prompt 格式化 |
| `legacy.py` | 旧 JSON/USER.md 的只读迁移适配，不再维护旧真值源 |
| `__init__.py` | 公开 API、组合根工厂和短期兼容门面 |

运行时相关接缝为：

- `turn_kernel.py`：工具结果语义分类和 `TurnVerificationState`；
- `agent_loop.py`：每个 Turn 唯一的记忆注入点，并把分类证据传给 Reflection；
- `agent_reflection.py`：只从共享验证状态生成最多一条 pending 经验候选；
- `main.py`、`headless.py`、TUI：创建或传递同一个 `MemoryService`；
- `cli_commands.py`：提供 `/memory` 生命周期命令，不拼接 Prompt；
- `context_compactor.py`：保留已有记忆块，不再重新检索；
- `cybernetic_orchestrator.py`：只接收同一个服务，不再拥有第二个 Injector/Pipeline。

## 3. SQLite 真值源和 schema

生产默认数据库只有：

```text
~/.repoterm/memory.sqlite3
```

测试必须显式传入临时 `db_path`。数据库当前 `SCHEMA_VERSION = 2`，核心表为：

```text
metadata(key, value)
memory_entries(
  id, scope, kind, key, content, status,
  project_key, branch_name,
  source_type, source_session_id, source_turn_id,
  version, supersedes_id, content_hash,
  signal_count, evidence_json,
  created_at, updated_at, expires_at
)
```

`signal_count` 用于累计同一隐式偏好或重复候选的独立信号；`evidence_json` 只保存最多 3 条有界证据摘要。SQLite 连接分为两类：

- 只读查询使用 `_connection()`，退出时显式 `close()`；
- 写操作使用 `transaction()`/`run_in_transaction()`，以 `BEGIN IMMEDIATE` 开始，并在成功时提交、异常时回滚、退出时关闭连接。

因此 Windows 下不会因为一个只读连接仍被引用而持续锁定数据库文件。schema 从旧版本升级时，字段变更和版本号更新也在同一个事务中完成。

## 4. 证据模型和质量门禁

### 4.1 `VerificationEvidence`

一条证据包含：

| 字段 | 含义 |
| --- | --- |
| `level` | `OBSERVATION`、`CHANGE`、`VALIDATION`、`CONFIRMATION` |
| `kind` | `READ_FILE`、`SEARCH`、`EDIT_FILE`、`TEST`、`BUILD`、`STATIC_CHECK`、`SCHEMA`、`FILE_ASSERT`、`RECOVERY` 等 |
| `tool_name` | 产生证据的工具名 |
| `ok` | 工具结果是否成功 |
| `summary` | 清洗后的摘要，最多 240 个字符 |
| `fingerprint` | 对规范化工具名、输入、状态和摘要计算的 SHA-256 |
| `source_session_id` | 可回到 Session Transcript 的引用 |
| `source_turn_id` | 可回到当前 Turn Trace 的引用 |
| `created_at` | 创建时间 |

摘要不保存完整 stdout/stderr、Diff、源码或思维内容，并再次经过敏感信息过滤。经验候选最多保存 3 条证据。

只有“成功的 `VALIDATION` 或 `CONFIRMATION`”能够支撑经验。一次 `read_file`、一次编辑成功或普通 shell 命令退出码为 0，都不能单独打开经验门禁。

### 4.2 工具语义分类

`turn_kernel.classify_tool_result()` 根据工具名和命令语义分类：

- `read_file`、`grep`、`search`、`list_files`：`OBSERVATION`；
- `edit_file`、`write_file`、`patch`、Diff、Checkpoint：`CHANGE`；
- Pytest/test runner、构建、lint/type-check、schema/migration、文件断言、recovery：对应的 `VALIDATION` 类型；
- 无法识别的普通 shell：默认 `OBSERVATION`。

`TurnVerificationState` 是本回合唯一的证据真值。`record_tool_result()` 只记录工具观察、错误和有界运行摘要；只有显式分类后的 `record_verification_evidence()` 才会改变验证状态。原始工具结果仍保留在 Transcript 中，Reflection 不从原始字符串自行推断“已验证”。

## 5. 写入与生命周期

### 5.1 用户明确记忆

`remember_explicit()` 用于用户明确要求长期保留的内容，经过 scope 和敏感信息检查后可直接写为 `active`。它不需要伪造验证证据。

### 5.2 经验候选

`propose_experience(content, evidence=...)` 强制检查：

- 证据非空、类型正确、最多 3 条；
- 至少一条成功的 `VALIDATION`/`CONFIRMATION`；
- 所有证据属于同一 Session 和 Turn；
- 正文同时包含可复用的条件、动作和验证结果；
- source session/turn 与证据一致；
- 结果固定为 `pending`，不能通过 `verified=True` 绕过。

Reflection 只在明确任务正常结束、有验证证据且没有 blocked、max_steps、verification_failed 或异常终止时调用该接口；每个 Turn 最多产生一个经验候选。模型只有 `assistant` 输出、没有工具验证证据时不会写入任何记忆。

### 5.3 决策、去重和冲突

写入前先作 `CREATE`、`UPDATE`、`ARCHIVE`、`DELETE` 或 `NOOP` 决策：

- 同 scope、同 content hash：`NOOP`，累计信号，不追加重复行；
- 同 scope、同 key、内容不同：旧 active 不被静默覆盖，新值保留为 pending 冲突候选；
- 用户确认更新：建立新版本，旧版本为 `superseded`；
- 用户修改经验正文：新版本清空旧证据，不能错误继承原验证；
- `archive` 可恢复，`purge` 仅在明确确认后彻底删除。

隐式偏好需要来自至少两个不同 Turn/Session 的信号；第一次只记录信号，第二个不同信号才产生一条 pending 候选，并通过 `signal_count` 累计，不盲目追加。

## 6. 迁移安全

`legacy.py` 支持旧 `memory.json` 和 `USER.md` 的只读迁移，但 SQLite 是唯一新真值源。

迁移每个来源前先扫描整个原始内容。如果发现 API Key、Token、Cookie、密码等敏感模式：

1. 整个来源拒绝导入；
2. 不写任何记录；
3. 不写 migration marker；
4. 报告只包含路径和安全错误，不包含敏感内容；
5. 用户清理来源后可以再次重试。

正常来源的所有记录插入和 marker 写入共用一个事务。任何中途数据库异常都会回滚全部记录和 marker，避免“导入了一半但以后无法重试”。旧文件不被覆盖或删除，其他独立来源仍可继续迁移。

## 7. 单库和单点注入

组合根创建服务：

```python
memory_service = create_memory_service(workspace=cwd, runtime=runtime)
```

Main、Headless 和 TUI 将这一个实例传入 Agent Loop、CLI 和 Reflection。Agent Loop 在当前 Turn 开始时调用唯一的 `MemoryInjector.inject_once()`；Injector 会：

- 只检索 `active` 且未过期的记录；
- 按确定性关键词和 scope 优先级排序；
- 最多选 5 条，受 Token 预算限制；
- 检测已有 `## Persistent Memory (advisory)`，保证幂等；
- 明确声明记忆不能覆盖系统指令、权限边界和工具安全规则。

TUI 不再预先检索，CLI 不参与 Prompt 拼接，ContextCompactor 不再调用 `get_relevant_context()`。压缩只保留已有 system message，因此最终 Prompt 中最多有一个 `## Persistent Memory` 块。没有服务时 Agent Loop 记录可观察 warning 并禁用持久化记忆，不创建工作区 fallback 数据库。

## 8. 命令和兼容层

`/memory` 支持：

```text
/memory status
/memory list
/memory pending
/memory approve <id>
/memory reject <id>
/memory update <id> <content>
/memory archive <id>
/memory restore <id>
/memory delete <id> --confirm
```

兼容导出的 `MemoryManager`、`MemoryScope`、`_tokenize` 和 `REPOTERM_DIR` 只服务于迁移期调用者；运行时委托到 SQLite `MemoryService`，不恢复 JSON/Markdown 第二真值源。旧 Pipeline、LLM Reranker、Vector、Curator、Timeline 和旧 Injector 模块已删除。

## 9. 验收入口

核心门禁：

```powershell
D:\Programfiles\Anacondafiles\envs\agent-env\python.exe -B -m pytest -q --tb=short -p no:cacheprovider tests/memory
D:\Programfiles\Anacondafiles\envs\agent-env\python.exe -B -m pytest -q --tb=short -p no:cacheprovider tests/contracts
```

运行时门禁：

```powershell
D:\Programfiles\Anacondafiles\envs\agent-env\python.exe -B -m pytest -q --tb=short -p no:cacheprovider tests/test_agentops_scenarios.py tests/test_compaction_robustness.py tests/test_agentops_proof_artifacts.py::test_memory_lifecycle_trace_updates_deletes_and_preserves_unrelated_memory tests/test_memory_e2e.py::TestCrossSessionMemoryContinuity::test_memory_survives_session_close_and_reopen
```

确定性 Runtime 回归：

```powershell
D:\Programfiles\Anacondafiles\envs\agent-env\python.exe benchmarks/runtime_regression_eval.py --rounds 3
```

该评测只使用脚本化模型，不调用真实 Provider。详细结果写入 `benchmarks/runtime_regression_results.json` 和 `.md`。

## 10. 已知边界

- 经验正文目前使用可审计的条件/动作/结果模板，不额外调用 LLM 做 Curator 或 Reranker。
- 检索是标准库确定性关键词检索，不使用 Embedding、向量数据库或第三方依赖。
- 证据摘要用于审计引用，不单独作为可检索 Prompt 内容。
- 用户明确记忆仍可能是用户主观偏好；系统只负责敏感信息、作用域和生命周期边界，不替用户重新解释授权。
- `service.py` 当前超过 500 行，主要原因是它同时承载生命周期业务规则和迁移期的 `MemoryManager` 兼容委托；兼容方法仍写入同一个 SQLite Service，没有恢复第二套存储或检索。后续若移除兼容 API，可再将兼容委托拆出或删除。

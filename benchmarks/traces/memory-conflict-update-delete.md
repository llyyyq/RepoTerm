# Trace：错误记忆的更新与删除

该场景不调用LLM，使用真实MemoryManager验证一条错误项目记忆从写入、命中、冲突、更新到删除的完整生命周期。

## 结果摘要

- Run ID：`memory-lifecycle-20260721T131737Z-c13cbe`
- Scope：`project`
- 查询：`项目测试命令`
- 是否调用Provider：否
- 最终结果：通过

## 生命周期Trace

| Seq | 动作 | 输入 | 输出/证据 |
| ---: | --- | --- | --- |
| 1 | 显式写入错误记忆 | {"command":"/memory add project: <错误测试命令>"} | {"message":"Saved memory (project): 项目测试命令使用 npm test，禁止运行 pytest。","entry":{"id":"project-1784639857-0","scope":"project","category":"note","content":"项目测试命令使用 npm test，禁止运行 pytest。","tier":"short_term","usage_count":0}} |
| 2 | 写入无关稳定记忆作为删除边界 | {"content":"Python代码格式化统一使用 ruff format。"} | {"entry":{"id":"project-1784639857-1","scope":"project","category":"convention","content":"Python代码格式化统一使用 ruff format。","tier":"short_term","usage_count":0}} |
| 3 | 检索并注入错误记忆 | {"query":"项目测试命令","token_budget":1000} | {"retrieved":[{"id":"project-1784639857-0","scope":"project","category":"note","content":"项目测试命令使用 npm test，禁止运行 pytest。","tier":"short_term","usage_count":2}],"prompt_context":"# Project Memory\n\n*Last updated: 2026-07-21 21:17*\n\n## Note\n\n- 项目测试命令使用 npm… |
| 4 | 检测新事实与旧记忆冲突 | {"candidate":"项目测试命令使用 pytest -q，不使用 npm test。","threshold":0.15} | {"conflicts":[{"entry":{"id":"project-1784639857-0","scope":"project","category":"note","content":"项目测试命令使用 npm test，禁止运行 pytest。","tier":"short_term","usage_count":2},"similarity":0.5769}]} |
| 5 | 更新错误记忆并重新加载 | {"entry_id":"project-1784639857-0","replacement":"项目测试命令使用 pytest -q，不使用 npm test。"} | {"updated":true,"retrieved_after_reload":[{"id":"project-1784639857-0","scope":"project","category":"note","content":"项目测试命令使用 pytest -q，不使用 npm test。","tier":"short_term","usage_count":4}],"prompt_context":"# Project Memory\n\n*Last updated: 2026-07-21 21:17… |
| 6 | 删除冲突记忆并再次重新加载 | {"entry_id":"project-1784639857-0"} | {"deleted":true,"retrieved_after_delete":[],"prompt_context":"","remaining_project_memories":["Python代码格式化统一使用 ruff format。"]} |

## Grader

| 判分项 | 结果 | 证据 |
| --- | --- | --- |
| 错误记忆由显式用户指令写入 | 通过 | Saved memory (project): 项目测试命令使用 npm test，禁止运行 pytest。 |
| 写入后检索和Prompt注入命中错误记忆 | 通过 | # Project Memory *Last updated: 2026-07-21 21:17* ## Note - 项目测试命令使用 npm test，禁止运行 pytest。 `chat` |
| 新事实触发旧记忆冲突检测 | 通过 | conflict_ids=['project-1784639857-0'] |
| 更新结果持久化并替换Prompt内容 | 通过 | # Project Memory *Last updated: 2026-07-21 21:17* ## Note - 项目测试命令使用 pytest -q，不使用 npm test。 `chat` |
| 删除后冲突记忆不再检索或注入 | 通过 | 最终Prompt上下文为空 |
| 删除仅影响目标记忆 | 通过 | remaining=['Python代码格式化统一使用 ruff format。'] |

## 边界

本样例验证显式写入、检索注入、冲突检测、更新、删除和重新加载；时间衰减与分层归档由独立单元测试覆盖，不把它们混入本Trace。


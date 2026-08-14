# M2.2 进程重启安全恢复交接

| 项目 | 内容 |
| --- | --- |
| 交接日期 | 2026-08-14 |
| 仓库状态 | `main` 已与 `origin/main` 同步，工作基线为合并提交 `1394502` |
| 已完成阶段 | M1 可运行闭环、M2.1 SQLite 持久化、合并前运行时契约加固 |
| 下一阶段 | M2.2：把进程中断的执行恢复为安全、可继续的状态 |
| GitHub 记录 | [PR #1](https://github.com/shuilingtianhua/ResearchFlow-agent/pull/1) 已合并 |

## 1. 当前结论

下一步先做 **M2.2 进程重启安全恢复**，暂不同时引入后台 Worker、SSE、Docker Sandbox 或真实模型。

当前 SQLite 已能在应用重建后读取 Run 快照和事件，但读取到 `RUNNING` 不代表原执行仍然存在。进程退出后，旧
`execution_id` 对应的执行者已经不可确认；如果直接继续使用旧身份，可能产生重复提交或让 Run 永久停在运行中。
M2.2 应先把这类状态原子转换成“可以由新执行身份继续”的安全状态，并保留恢复事件证据。

需求来源和完整验收标准直接参见：

- [`PRD FR-023 持久化与恢复`](../PRD.md#fr-023-持久化与恢复p0)
- [`PRD AC-04 服务重启恢复`](../PRD.md#ac-04-服务重启恢复)
- [`ARCHITECTURE 并发与取消`](../ARCHITECTURE.md#7-并发与取消)
- [`M2.1 SQLite 持久化实施记录`](../development/2026-08-14_m2_sqlite_persistence.md)
- [`合并前运行时契约加固记录`](../development/2026-08-14_pre_merge_runtime_hardening.md)

## 2. M2.2 建议交付边界

完成后至少应满足：

1. 应用启动时能发现持久化的未终态 Run；首个垂直切片优先处理 `RUNNING`。
2. 中断前为 `RUNNING` 的 Task 转回可重试状态，旧 `execution_id` 被清除并永久失效。
3. 已经 `SUCCEEDED` 的 Task、输出和事件保持不变，恢复后不得重复执行。
4. Run 先进入明确的安全状态，建议使用 `PAUSED`；用户调用现有 Resume 后，以新的 `execution_id` 继续。
5. 快照变化和恢复事件通过 `RunStore.commit()` 原子提交；并发恢复发生版本冲突时重新加载并有限重试。
6. 重复启动恢复必须幂等，不能重复追加恢复事件或重复增加 Attempt。
7. 数据库损坏继续显式报错，不能用空状态覆盖现场。

建议新增 `run.recovered` 事件，载荷记录恢复前状态、恢复原因和失效的执行数量。不要把进程中断伪装成任务失败，
也不要复用旧执行身份。

本阶段先实现“恢复到可安全继续的状态”。后台自动领取、心跳续租、自动继续和 SSE 推送放在 M2.3；这样可以先独立
证明恢复状态机正确，再扩大并发面。

## 3. 推荐实现顺序

1. 先在 `docs/development/` 基于模板建立 M2.2 实施记录，写清状态转换表和非目标。
2. 为 `RunStore` 增加窄接口，用于枚举需要恢复的快照；同时实现 InMemory 与 SQLite 适配器。
3. 在 Runtime 内实现恢复状态迁移，不让 FastAPI 或 SQLite 适配器拥有业务规则。
4. 通过 FastAPI lifespan 或组合根触发一次启动恢复，保持依赖组装仍集中在 `bootstrap.py`。
5. 补齐状态、幂等、并发冲突、SQLite 重启和恢复后 Resume 的测试。
6. 全量验证通过后，更新开发记录和本交接索引，再决定是否进入 M2.3。

## 4. 代码接手入口

| 位置 | 当前职责 | M2.2 关注点 |
| --- | --- | --- |
| [`runtime/store.py`](../../src/researchflow/runtime/store.py) | Runtime 拥有的存储端口 | 增加最小的可恢复快照枚举能力 |
| [`runtime/service.py`](../../src/researchflow/runtime/service.py) | Run 状态机和原子事件提交 | 恢复迁移、冲突重判、旧执行身份失效 |
| [`domain/event.py`](../../src/researchflow/domain/event.py) | 事件类型 | 定义可审计的恢复事件 |
| [`adapters/persistence/sqlite.py`](../../src/researchflow/adapters/persistence/sqlite.py) | SQLite 快照与事件事实来源 | 查询未终态快照，保持损坏数据显式失败 |
| [`adapters/persistence/memory.py`](../../src/researchflow/adapters/persistence/memory.py) | 测试用内存 Store | 与 SQLite 遵守同一端口契约 |
| [`bootstrap.py`](../../src/researchflow/bootstrap.py) | 生产依赖组合根 | 组装启动恢复流程 |
| [`api/app.py`](../../src/researchflow/api/app.py) | HTTP 边界 | 仅承载 lifespan 触发，不写恢复规则 |
| [`tests/runtime/test_service.py`](../../tests/runtime/test_service.py) | 状态机行为测试 | 恢复、幂等、迟到结果和新执行身份 |
| [`tests/adapters/test_sqlite_run_store.py`](../../tests/adapters/test_sqlite_run_store.py) | SQLite 契约测试 | 重启枚举、并发提交和损坏数据 |

## 5. 必测场景

- 预置一个包含“已成功 Task + 正在运行 Task”的 SQLite Run，重建应用后只重置正在运行的 Task。
- 恢复后 Run 处于 `PAUSED`，旧执行 ID 清空；Resume 后生成新执行 ID，并且成功 Task 不重复执行。
- 同一数据库连续启动两次，第二次不产生额外恢复事件。
- 两个恢复者竞争同一 Run 时，仅一个原子提交成功，另一个重新加载后得到幂等结果。
- 恢复前返回的迟到结果不能覆盖恢复后的状态，并留下 `task.result_ignored`。
- 非法快照或损坏数据库仍返回项目错误，不静默跳过。

完成前执行：

```powershell
python -m pytest -q -W error::DeprecationWarning
python -m mypy src/researchflow
python -m ruff check .
python -m ruff format --check .
python -m compileall -q src tests
```

当前合并基线为 `32 passed`；后续只能把新一轮实际执行结果写入开发记录。

## 6. 风险与非目标

- SQLite 仍是单机 MVP，不在 M2.2 声称支持多节点分布式调度。
- 当前 `runs` 表只保存版本与 JSON 快照；是否增加可索引状态列应由实际查询规模决定，不要仅为“以后可能需要”扩表。
- 不在恢复适配器里直接修改状态；状态规则属于 Runtime。
- 不自动重跑 `SUCCEEDED` Task，不删除旧事件，不篡改旧 Attempt 证据。
- 不在本阶段顺带实现 Worker 心跳、远程租约、SSE、模型或 Sandbox。

## 7. 分支与协作约定

- 不为文档补充、小修复或每个测试单独创建分支。
- 真正开始 M2.2 代码改动时，如受 `main` 保护规则约束，只建立 **一个 M2.2 里程碑分支**，整个阶段在同一分支和 PR
  中推进，不按子步骤反复开分支。
- 未开始编码前无需提前创建空分支。

## 8. Suggested skills

后续接手 Agent 建议按需使用：

- `ai-pair-programmer`：阅读现有项目约定并完成 Python 实现与验证。
- `domain-modeling`：在编码前明确 Recover、Resume、Attempt、Lease 和迟到结果的业务语义。
- `codebase-design`：确定恢复逻辑、存储枚举端口和应用 lifespan 的模块边界。
- `tdd`：优先用失败测试固定重启、幂等和并发冲突场景。
- `code-review`：合入前按 Standards 与 Spec 两个维度复核。

## 9. 接手时第一条指令

> 基于 `main` 当前 M1 + M2.1 基线，先阅读 `docs/handoffs/2026-08-14_m2_2_recovery_handoff.md`，建立 M2.2
> 开发记录和失败测试，实现进程重启后 RUNNING Attempt 的安全失效与可恢复状态转换；不要同时实现 Worker、SSE、
> Docker 或真实模型。

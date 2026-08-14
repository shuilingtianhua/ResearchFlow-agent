# M2.4 Run 内并行 DAG 调度交接

| 项目 | 内容 |
| --- | --- |
| 交接日期 | 2026-08-14 |
| 仓库状态 | `main@76f8ebf`；M2.3 位于本地未提交工作区 |
| 已完成阶段 | M1、M2.1 SQLite、M2.2 启动恢复、M2.3 后台 Worker 与 SSE |
| 验证基线 | `46 passed`，mypy、Ruff、格式与 compileall 全部通过 |
| 下一阶段 | M2.4：受预算约束的 Run 内并行 DAG 调度 |

## 1. 当前结论

M2.3 已把 HTTP 提交与执行解耦。FastAPI lifespan 启动单进程 Worker，创建和 Resume 返回持久化的 `RUNNING` 状态；Worker 通过乐观锁、新 `execution_id` 和单调 `lease_epoch` 领取 Task。不同 Run 可以并发执行，Worker 关闭后的现场由 M2.2 恢复。SSE 直接跟随持久化事件并支持 `after_sequence`。

当前同一 Run 内仍一次选择一个可运行 Task，因此尚未满足 FR-020 对无依赖冲突 Step 并行执行的完整要求。下一步只收口这一项运行治理能力，不同时接入 Docker、真实模型或 Artifact Store。

## 2. 推荐交付顺序

1. 建立 M2.4 开发记录，冻结并行宽度、失败传播和预算语义。
2. 用两个互不依赖的阻塞 Capability 证明它们能同时开始，且并发数不超过 `RunBudget.max_concurrency`。
3. 在 Runtime 内批量识别可运行 Task，并用 AnyIO TaskGroup 结构化执行；每个 Task 仍独立原子领取。
4. 覆盖一个并行分支失败时，依赖后代 Blocked、其他在途结果失效且留下证据的语义。
5. 覆盖并行执行期间 Pause、Cancel、超时、Run 墙钟预算和 Worker 关闭。
6. 全量验证后再进入 M3 Artifact Store、Docker 和真实 Capability。

## 3. 接手入口

| 位置 | 关注点 |
| --- | --- |
| [`runtime/service.py`](../../src/researchflow/runtime/service.py) | `_execute_until_stopped` 当前按单 Task 推进 |
| [`domain/run.py`](../../src/researchflow/domain/run.py) | `RunBudget.max_concurrency` |
| [`runtime/store.py`](../../src/researchflow/runtime/store.py) | 乐观锁与可运行快照枚举 |
| [`tests/runtime/test_service.py`](../../tests/runtime/test_service.py) | DAG、失败传播和状态竞争 |
| [`tests/integration/test_recovery.py`](../../tests/integration/test_recovery.py) | Worker 竞争、关闭恢复和 SQLite |
| [`M2.3 实施记录`](../development/2026-08-14_m2_3_background_worker_sse.md) | 已完成接口与限制 |

## 4. 风险与非目标

- 并行 Task 会同时提交快照，必须重新加载并有限重试，不能共享可变 Run 对象。
- 一个分支失败后的其他在途结果必须按执行身份重判，不能覆盖 Run 终态。
- `max_concurrency` 是单 Run 上限；不要在本阶段扩展为集群级配额系统。
- 不引入裸 `asyncio.create_task` 作为生产调度机制，继续使用 AnyIO 结构化并发。
- 不同时实现远程 Worker、心跳续租、Docker、真实模型或 Artifact Store。

## 5. 接手时第一条指令

> 基于已验证的 M2.3 Worker 与 SSE 契约，先建立 M2.4 开发记录和失败测试，让同一 Run 内两个独立 Task 在 `max_concurrency=2` 时同时开始，并保持失败、暂停、取消、预算和迟到结果语义；不要同时接入 Docker 或真实模型。

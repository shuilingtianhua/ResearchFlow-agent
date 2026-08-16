# M2.4 Run 内并行 DAG 调度实施记录

| 项目 | 内容 |
| --- | --- |
| 状态 | 已实现并验证 |
| 所属里程碑 | `M2.4` |
| 创建日期 | `2026-08-14` |
| 最后验证日期 | `2026-08-14` |
| 关联需求 | `docs/PRD.md` FR-020、FR-021、NFR 10.2 |
| 关联架构 | `docs/ARCHITECTURE.md` Runtime 与可信执行边界 |
| 代码版本 | `main` 本地工作区，基线 `8d278b2` |

## 1. 需求

同一 Run 中依赖已经满足且互不冲突的 Task，应在 `RunBudget.max_concurrency`
限制内并行执行。每个 Task 仍需独立原子领取、携带新的 `execution_id` 与单调
`lease_epoch`，并保持失败、暂停、取消、超时、墙钟预算和迟到结果隔离语义。

## 2. 业务背景与问题

M2.3 已将提交与执行解耦，并允许 Worker 并发处理不同 Run；但当前执行循环看到任意
在途 Task 就停止推进，导致单个 Run 的无依赖分支仍然串行，未完整满足 FR-020。

## 3. 范围与非目标

### 本步骤范围

- 批量识别可运行 Task，并以 AnyIO 结构化并发执行。
- 按单 Run 的 `max_concurrency` 限制全局在途 Task 数量。
- 通过乐观锁保证跨 Worker 原子领取，并保留现有 Attempt 身份校验。
- 覆盖并行成功、失败传播、暂停/取消、超时、墙钟预算和 Worker 停机恢复。

### 非目标

- 不实现集群级配额、心跳续租或远程 Worker。
- 不接入 Docker、真实模型、Artifact Store 或 Git Workspace。
- 不改变 Runtime 的命令语义；仅补充进程生命周期所需的显式 `close()`。

## 4. 当前基线与参考

| 能力 | 当前状态 | 证据或说明 |
| --- | --- | --- |
| 后台 Worker 与跨 Run 并行 | 已实现 | `runtime/service.py::run_worker` |
| 原子 Task 领取 | 已实现 | 快照版本乐观锁、`execution_id`、`lease_epoch` |
| 单 Run 并行 | 不支持 | `_execute_until_stopped` 遇到在途 Task 即返回 |
| 暂停/取消迟到结果隔离 | 已实现 | Attempt 身份重判与 `task.result_ignored` |

## 5. 方案与解决思路

测试 seam 固定为 `RuntimeService` 公共命令接口及其返回的持久化快照/事件。Runtime
内部顺序完成 Task 领取提交，再把已领取执行交给 AnyIO TaskGroup；每次领取前重新加载
快照并计算当前在途数量，因此多个 Worker 竞争同一 Run 时仍不会突破单 Run 上限。

失败分支沿用现有 Run 终态转换：失败节点为 `FAILED`，其后代为 `BLOCKED`，其他未完成
节点为 `CANCELLED`；已经在途的结果返回后因执行身份失效而记录忽略事件。

## 6. 实现说明

| 模块或文件 | 职责 | 主要改动 |
| --- | --- | --- |
| `runtime/service.py` | Run 调度与 Task 执行 | 顺序原子领取、AnyIO TaskGroup 并发执行、按当前 `RUNNING` 数量限流 |
| `runtime/contracts.py`、`runtime/store.py` | 生命周期契约 | 增加显式 `close()`，保证应用停机释放持久化资源 |
| `adapters/persistence/memory.py` | 测试 Store | 提供无副作用关闭实现 |
| `adapters/persistence/sqlite.py` | SQLite Store | 屏蔽单次数据库操作的外层取消，确保事务和连接完成清理 |
| `api/app.py` | 应用生命周期 | Worker TaskGroup 退出后关闭 Runtime |
| `tests/runtime/test_service.py` | Runtime 行为验证 | 覆盖并行宽度、上限、DAG 汇合、失败、Pause 和 Cancel |
| `tests/integration/test_recovery.py` | SQLite/Worker 验证 | 覆盖双 Worker 单次领取和多个在途 Attempt 停机恢复 |
| `tests/api/test_app.py` | 应用生命周期验证 | 证明 lifespan 会关闭 Runtime Store |

## 7. 技术难点与解决方案

### 7.1 原子领取与并行执行的边界

- 现象：若直接并发提交多个由同一快照派生的状态，只有一个乐观锁提交能成功，无法形成并行；若先调用 Capability 再提交，又会重复执行。
- 根因：领取提交和外部执行必须分离，并且领取仍要按最新快照串行推进。
- 最终方案：`_claim_task()` 先提交 `RUNNING`、`execution_id` 和 `lease_epoch`，成功后生成不可变 `_TaskExecution`；TaskGroup 只并发执行已经领取的 Attempt。
- 选择理由：状态写入保持串行、可审计，慢 Capability 获得并行度，且无需暴露新的公共调度接口。

### 7.2 多 Worker 下的单 Run 上限

- 现象：不同 Worker 可能同时枚举到一个没有在途 Task 的 Run。
- 根因：进程内 `active_run_ids` 只能去重本 Worker，不能作为跨 Worker 配额。
- 最终方案：每次领取前重新加载持久化快照，以全局 `RUNNING` Task 数计算剩余槽位；领取仍以版本提交决定唯一赢家。
- 残余风险：当前是单机 SQLite 竞争语义，没有集群级心跳续租或公平调度。

### 7.3 停机取消与 SQLite 清理

- 现象：并行 Worker 停机可能恰好取消 SQLite 查询或提交，导致 Windows 上出现临时写锁、aiosqlite 后台线程向已关闭事件循环回调。
- 根因：外层 TaskGroup 取消传播到了数据库操作的事务/连接清理阶段。
- 最终方案：SQLite Adapter 用取消屏蔽完成每次有限数据库操作；FastAPI lifespan 在 Worker TaskGroup 退出后显式关闭 Runtime/Store。
- 选择理由：取消仍能在当前有限数据库操作完成后传播，同时不会遗留连接或破坏已承诺的原子提交。

### 7.4 SQLite 首次建表竞态

- 现象：两个全新 Store 同时首次访问同一数据库时，`create_all(checkfirst)` 仍可能发生检查后建表竞态。
- 当前处理：竞争 Worker 测试先顺序初始化 schema，再开始调度竞争。
- 残余风险：生产级多进程 schema 迁移与启动锁不属于本阶段，已写入 M3.1 交接限制。

## 8. 验证与证据

| 检查 | 实际命令 | 结果 | 证据位置 |
| --- | --- | --- | --- |
| 全量测试 | `python -m pytest -q -W error::DeprecationWarning` | 通过，`55 passed in 7.69s` | `tests/` |
| Runtime/API/Worker 定向测试 | `python -m pytest tests/integration/test_recovery.py tests/api/test_app.py -vv -W error::DeprecationWarning` | 通过，`16 passed in 5.51s` | `tests/integration/`、`tests/api/` |
| 类型检查 | `python -m mypy src/researchflow` | 通过，34 个源文件无问题 | `src/researchflow/` |
| 静态检查 | `python -m ruff check .` | 通过 | 全仓库 |
| 格式检查 | `python -m ruff format --check .` | 通过，57 个文件已格式化 | 全仓库 |
| 字节码编译 | `python -m compileall -q src tests` | 通过 | `src/`、`tests/` |
| Diff 完整性 | `git diff --check` | 通过 | 当前工作区 |

未运行或受限的检查：无。

## 9. 当前限制、风险与非承诺

- `max_concurrency` 是单 Run 上限，不代表进程或集群总配额。
- 无心跳续租；进程中断仍由启动恢复将 Run 转为安全暂停态。
- Run 内任务按计划顺序尝试领取，不承诺优先级、公平性或工作窃取。
- SQLite 多进程首次 schema 初始化仍需独立迁移/启动锁治理。

## 10. 下一步

1. 进入 M3.1，先实现内容寻址 Artifact Store 与 Runtime 产物闭环。
2. Artifact 可验证并可持久化恢复后，再接入 Docker Sandbox 与真实 Capability。

## 11. 变更历史

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-08-14 | 创建记录并冻结测试 seam 与并发语义 | ResearchFlow Agent 团队 |
| 2026-08-14 | 完成并行调度、竞争/失败/控制/恢复测试与 SQLite 停机清理 | ResearchFlow Agent 团队 |

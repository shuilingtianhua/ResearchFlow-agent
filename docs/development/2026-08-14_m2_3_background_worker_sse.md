# M2.3 后台执行与 SSE 实施记录

| 项目 | 内容 |
| --- | --- |
| 状态 | 已实现并验证 |
| 所属里程碑 | `M2.3` |
| 创建日期 | `2026-08-14` |
| 最后验证日期 | `2026-08-14` |
| 关联需求 | [`PRD FR-020`](../PRD.md#fr-020-dag-调度p0)、[`FR-022`](../PRD.md#fr-022-取消与人工重试p0)、[`FR-050`](../PRD.md#fr-050-实时事件流p0) |
| 关联架构 | [`ARCHITECTURE 5.1`](../ARCHITECTURE.md#51-research-runtime)、[`7. 并发与取消`](../ARCHITECTURE.md#7-并发与取消) |
| 代码版本 | `main@76f8ebf`，本地工作区 |

## 1. 需求

把 Run 的提交与执行解耦：HTTP 创建或恢复 Run 后立即返回持久化状态，单进程后台 Worker 领取可执行 Run 并完成 DAG；客户端可以通过 SSE 接收已经原子提交的实时事件，并用事件序号断点续读。

验收条件：

- `POST /runs` 不等待慢 Capability 完成，返回时 Run 已持久化并可查询。
- Worker 只执行 `RUNNING` Run；两个 Worker 竞争时同一个 Task 只被一个新 `execution_id` 领取。
- Pause、Cancel 和进程关闭继续使迟到结果失效；关闭中断由 M2.2 启动恢复兜底。
- SSE 使用与历史查询相同的事件事实来源，按单 Run 序号递增推送。
- SSE 支持 `after_sequence`，已有 JSON 事件查询接口保持兼容。

## 2. 业务背景与问题

当前 `POST /runs` 在请求协程内执行完整 DAG，慢实验会长期占用 HTTP 请求，也无法在执行过程中稳定建立事件流或由独立 Worker 管理生命周期。M2.2 已保证中断恢复安全，因此可以在不引入远程队列和多节点调度的前提下，先完成单进程后台执行闭环。

## 3. 范围与非目标

### 本步骤范围

- HTTP 使用非阻塞提交语义；保留 Runtime 原同步 `dispatch()` 兼容入口。
- Runtime 内部单进程 Worker 与结构化并发生命周期。
- 使用快照版本和新 `execution_id` 作为 Task 领取租约。
- SSE 实时事件、断点续读和历史 JSON 查询兼容。

### 非目标

- 远程 Worker、消息队列、跨节点心跳和租约续期。
- Docker、Git Workspace、真实模型、PostgreSQL 或 Artifact Store。
- 自动 Resume 用户暂停或启动恢复后处于 `PAUSED` 的 Run。

## 4. 当前基线与参考

| 能力 | 当前状态 | 证据或说明 |
| --- | --- | --- |
| 同步 DAG 执行 | 已实现 | `RuntimeService.dispatch()` |
| SQLite 原子快照与事件 | 已实现 | M2.1 |
| 进程中断安全恢复 | 已实现 | M2.2 |
| 后台 Worker | 未实现 | HTTP 当前等待完整执行 |
| SSE | 未实现 | 仅有历史 JSON 事件查询 |

## 5. 方案与解决思路

Runtime 保持一个深模块：调用方只需要知道同步 `dispatch()`、非阻塞 `submit()`、生命周期 `run_worker()` 和事件订阅。Worker 的轮询、活动 Run 去重、领取竞争和取消全部留在实现内部，不新增只有一个实现的 Worker Protocol。

`submit(StartRun)` 完成创建、规划和进入 `RUNNING`；`submit(ResumeRun)` 只完成恢复到 `RUNNING`。Worker 枚举可运行快照并执行，Task 开始事件中的全新 `execution_id` 是当前单进程阶段的领取租约；原子提交失败的竞争者不得调用 Capability。

SSE 轮询已经提交的事件流，不读取进程内临时队列，因此重连、重启和历史查询共享同一个事实来源。

## 6. 实现说明

| 模块或文件 | 职责 | 主要改动 |
| --- | --- | --- |
| `runtime/service.py` | 命令、Worker、领取与实时事件 | 增加非阻塞提交、结构化 Worker、竞争保护和事件跟随 |
| `runtime/contracts.py` | 稳定 Runtime 接口 | 增加 `submit()`、`run_worker()` 和 `watch_events()` |
| `capabilities/contracts.py` | Capability 执行契约 | 增加单调 `lease_epoch` |
| `api/app.py` | HTTP/SSE 协议和 lifespan | lifespan 启动 Worker，创建/Resume 非阻塞，新增 SSE 路由 |
| `tests/runtime/`、`tests/integration/` | 状态、竞争和关闭语义 | 覆盖双 Worker 单次领取与关闭后恢复 |
| `tests/api/` | 非阻塞 HTTP、动作竞争与 SSE | 覆盖慢 Capability、Pause/Cancel 和断点续读 |

## 7. 技术难点与解决方案

### 7.1 多 Worker 重复执行

- 现象：多个 Worker 可能同时枚举到同一个 `RUNNING` Run。
- 根因：枚举不是领取，读取和 Task 开始提交之间存在竞争窗口。
- 最终方案：以 Task `RUNNING` + 新 `execution_id` 的乐观锁提交作为领取点；只有提交成功者调用 Capability。
- 残余风险：没有跨节点心跳和过期续租，不声明分布式 Worker 能力。

### 7.2 实时事件与持久化一致性

- 现象：若 SSE 使用独立内存队列，重启或断线后可能丢事件或乱序。
- 最终方案：SSE 仅轮询 `RunStore` 已提交事件，用单调序号续读。

## 8. 验证与证据

| 检查 | 实际命令 | 结果 | 证据位置 |
| --- | --- | --- | --- |
| 全量测试 | `python -m pytest -q -W error::DeprecationWarning` | 通过，`46 passed in 5.20s` | `tests/` |
| 类型检查 | `python -m mypy src/researchflow` | 通过，34 个源文件无问题 | `src/researchflow/` |
| 静态检查 | `python -m ruff check .` | 通过 | 全仓库 |
| 格式检查 | `python -m ruff format --check .` | 通过，55 个文件已格式化 | 全仓库 |
| 字节码编译 | `python -m compileall -q src tests` | 通过 | `src/`、`tests/` |

未运行或受限的检查：无。

## 9. 当前限制、风险与非承诺

- SQLite Worker 仅面向单机 MVP。
- 进程关闭时允许当前执行被取消并保留 `RUNNING` 现场，下一次启动由 M2.2 原子恢复。
- SSE 延迟受持久化查询轮询间隔影响，不承诺远程消息系统级吞吐。
- Worker 可以并发处理多个 Run，但同一 Run 内独立 Task 仍按顺序执行。
- 当前租约没有跨节点心跳和到期续租，不声明分布式 Worker 能力。

## 10. 下一步

1. 进入 M2.4，实现受 `RunBudget.max_concurrency` 约束的 Run 内并行 DAG 调度。
2. 补齐并行 Task 失败、Pause/Cancel、预算和关闭竞争测试后，再进入真实 Artifact/Sandbox。

## 11. 变更历史

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-08-14 | 创建记录，冻结 Worker、领取租约和 SSE 边界 | ResearchFlow Agent 团队 |
| 2026-08-14 | 完成非阻塞提交、后台 Worker、领取租约、关闭恢复与 SSE 验证 | ResearchFlow Agent 团队 |

# M2.2 进程重启安全恢复实施记录

| 项目 | 内容 |
| --- | --- |
| 状态 | 已实现并验证 |
| 所属里程碑 | `M2.2` |
| 创建日期 | `2026-08-14` |
| 最后验证日期 | `2026-08-14` |
| 关联需求 | [`PRD FR-023`](../PRD.md#fr-023-持久化与恢复p0)、[`PRD AC-04`](../PRD.md#ac-04-服务重启恢复) |
| 关联架构 | [`ARCHITECTURE 6. 数据与一致性`](../ARCHITECTURE.md#6-数据与一致性)、[`7. 并发与取消`](../ARCHITECTURE.md#7-并发与取消) |
| 代码版本 | M2.2 交付提交，具体版本见 Git 历史 |

## 1. 需求

应用启动时发现持久化的 `RUNNING` Run，将进程中断后已经失去执行者的 Task 原子恢复为可安全继续的状态。恢复后保留已成功 Task、输出、Attempt 和历史事件；清除旧执行身份；用户 Resume 后只继续未完成部分。

验收条件：

- `RUNNING` Run 恢复为 `PAUSED`，其中 `RUNNING` Task 恢复为 `READY`。
- 已成功 Task 和输出保持不变，恢复本身不增加 Attempt。
- 旧 `execution_id` 永久失效，并留下 `run.recovered` 事件。
- 重复启动幂等；并发恢复只允许一次原子提交。
- SQLite 重建应用后自动执行恢复；损坏数据继续显式失败。

## 2. 业务背景与问题

SQLite 已能跨进程保存和读取 Run，但快照中的 `RUNNING` 只表示中断前状态，不能证明旧进程中的执行者仍然存活。若复用旧执行身份或把 Run 永久留在运行态，会造成迟到结果覆盖、重复提交或无法继续执行。

## 3. 范围与非目标

### 本步骤范围

- 启动时枚举需要恢复的 `RUNNING` Run。
- Runtime 拥有恢复状态迁移、执行身份失效、事件和冲突重判规则。
- InMemory 与 SQLite Store 遵守相同枚举契约。
- FastAPI lifespan 只触发一次恢复，不承载状态规则。

### 非目标

- 后台 Worker、自动领取、心跳或远程租约。
- 自动 Resume、SSE、Docker Sandbox 或真实模型。
- 多节点分布式调度或 PostgreSQL。

## 4. 当前基线与参考

| 能力 | 当前状态 | 证据或说明 |
| --- | --- | --- |
| Run 快照和事件持久化 | 已实现 | `adapters/persistence/sqlite.py` |
| 暂停时失效运行身份 | 已实现 | `RuntimeService._paused_transition()` |
| 迟到结果隔离 | 已实现 | `RuntimeService._is_active_execution()` |
| 启动恢复 | 已实现 | `RuntimeService.recover_interrupted_runs()` 与 FastAPI lifespan |

## 5. 方案与解决思路

状态转换表：

| 恢复前 | 恢复后 | 保留 | 清除/新增 |
| --- | --- | --- | --- |
| Run `RUNNING` | Run `PAUSED` | Plan、成功 Task、输出、错误、Attempt、历史事件 | 清除旧执行 ID，新增一次 `run.recovered` |
| Task `RUNNING` | Task `READY` | Attempt 计数 | 清除该进程遗留的执行身份 |
| Task `SUCCEEDED` | 不变 | 状态和输出 | 不重复执行 |
| 非 `RUNNING` Run | 不变 | 全部 | 不追加恢复事件 |

Runtime 先通过消费方定义的窄 Store 端口枚举 `RUNNING` 快照，再逐个重新加载并执行乐观锁提交。发生版本冲突时有限重试并重新判断；其他恢复者已经完成转换时视为幂等成功，不再追加事件。

## 6. 实现说明

| 模块或文件 | 职责 | 主要改动 |
| --- | --- | --- |
| `runtime/store.py` | Runtime 拥有的存储端口 | 增加按 Runtime 指定状态枚举快照的窄接口 |
| `runtime/service.py` | 恢复状态机 | 实现状态迁移、幂等、有限冲突重判和恢复计数 |
| `domain/event.py` | 审计事件 | 增加 `run.recovered` |
| `adapters/persistence/` | Store 实现 | InMemory 与 SQLite 实现统一枚举契约 |
| `api/app.py` | 进程生命周期 | FastAPI lifespan 触发组合根创建的 Runtime 恢复；恢复规则不进入 API 层 |
| `bootstrap.py` | 生产依赖组装 | 继续作为唯一组合根，无需新增恢复规则或平行依赖构造 |

## 7. 技术难点与解决方案

### 7.1 恢复者竞争与重复启动

- 现象：两个进程可能同时读取同一个 `RUNNING` 快照。
- 根因：枚举和提交之间存在乐观锁竞争窗口。
- 候选方案：数据库悲观锁；适配器直接改状态；Runtime 乐观锁重判。
- 最终方案：Runtime 使用现有版本号和 `RunStore.commit()` 有限重试。
- 选择理由：保持业务规则在 Runtime，并复用现有原子提交契约。
- 残余风险：SQLite 仍只承诺单机 MVP，不扩展为分布式租约。

## 8. 验证与证据

| 检查 | 实际命令 | 结果 | 证据位置 |
| --- | --- | --- | --- |
| 全量测试 | `python -m pytest -q -W error::DeprecationWarning` | 通过，`40 passed in 2.21s` | `tests/runtime/`、`tests/adapters/`、`tests/api/`、`tests/integration/` |
| 类型检查 | `python -m mypy src/researchflow` | 通过，34 个源文件无问题 | `src/researchflow/` |
| 静态检查 | `python -m ruff check .` | 通过 | 全仓库 |
| 格式检查 | `python -m ruff format --check .` | 通过，52 个文件已格式化 | 全仓库 |
| 字节码编译 | `python -m compileall -q src tests` | 通过 | `src/`、`tests/` |

未运行或受限的检查：无。

## 9. 当前限制、风险与非承诺

- 恢复只把现场转换为可安全继续状态，不自动执行 Resume。
- 当前快照表没有独立状态列；先以正确性为主，不为未来规模提前扩表。
- 不声明多节点调度、租约续期或执行者存活检测能力。

## 10. 下一步

1. 进入 M2.3，先交付进程内后台 Worker、领取/租约与非阻塞执行的最小闭环。
2. 在后台执行边界稳定后接入 SSE 实时事件推送。

## 11. 变更历史

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-08-14 | 创建 M2.2 实施记录，冻结状态转换和非目标 | ResearchFlow Agent 团队 |
| 2026-08-14 | 完成启动恢复、幂等、竞争、Resume、迟到结果和损坏数据验证 | ResearchFlow Agent 团队 |

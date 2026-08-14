# M2.1 SQLite 持久化实施记录

| 项目 | 内容 |
| --- | --- |
| 状态 | 已实现并验证 |
| 所属里程碑 | M2：持久化与运行治理 |
| 创建日期 | 2026-08-14 |
| 最后验证日期 | 2026-08-14 |
| 关联需求 | [PRD FR-023 持久化与恢复](../PRD.md) |
| 关联架构 | [架构设计第 6 节：数据与一致性](../ARCHITECTURE.md) |
| 代码版本 | `agent/m1-runtime-agents-guide`，本记录随 M2.1 实现提交发布 |

## 1. 需求

用 SQLite 替换生产组合根中的内存 RunStore，使 Run 快照和事件在进程退出后仍然存在，
同时保持 M1 已建立的原子提交、乐观锁和单 Run 事件序号契约。

本步骤验收行为：

- 创建和提交的 Run 快照、任务状态、计划、输出和预算可以完整反序列化。
- 新建另一个 Store 或应用实例后，仍能查询之前的 Run 和事件。
- 快照与本次事件在同一数据库事务内提交，任一部分失败时都不留下部分写入。
- 并发提交使用 `expected_version` 检测陈旧版本。
- 重复 Run ID、未知 Run 和损坏数据库返回明确错误，不静默覆盖或创建空状态。
- 应用默认使用 SQLite，测试仍可显式使用隔离的临时数据库或内存 Store。

## 2. 业务背景与问题

M1 的内存 Store 只能证明执行循环：服务一旦重启，Run 当前状态、任务输出和审计事件全部
丢失。这使长时间科研任务无法查询历史，更无法在后续实现安全恢复。持久化是后台执行、
SSE 重连、租约和恢复策略的共同前置条件，因此 M2 先稳定数据事实来源，再引入并发 Worker。

## 3. 范围与非目标

### 本步骤范围

- 基于 SQLAlchemy Async Core 和 aiosqlite 实现 `SQLiteRunStore`。
- 使用 SQLite 表保存版本化快照和追加式事件流。
- 使用 Pydantic TypeAdapter 对现有不可变 dataclass 做 JSON 边界转换。
- 将 SQLAlchemy 和 aiosqlite 纳入默认运行依赖，并增加数据库 URL 配置。
- 沿用内存 Store 的相同契约场景，增加跨 Store/应用实例持久化和损坏库测试。
- 更新组合根、README、架构状态和本实施记录。

### 非目标

- 本步骤不恢复重启前处于 `RUNNING` 的任务，也不自动重新调度。
- 不实现后台 Worker、租约、心跳、并发度控制和 SSE。
- 不做 SQLite 到 PostgreSQL 的数据迁移。
- 不把大文件或 Artifact 正文写入数据库。
- 不实现数据库 schema 自动升级工具；M2.1 仅建立首版 schema。

## 4. 当前基线与参考

| 能力 | 开发前状态 | 本步骤目标 |
| --- | --- | --- |
| `RunStore` 端口 | 已实现 | 保持接口不变 |
| 内存 Store | 已实现 | 保留为单元测试替身 |
| SQLite 快照 | 未实现 | 实现并接入组合根 |
| SQLite 事件流 | 未实现 | 与快照同事务提交 |
| 重启后查询 | 未实现 | 支持新 Store/应用实例读取 |
| 未完成任务恢复 | 未实现 | 后续 M2.2 处理 |

架构文档已经选择 SQLite + SQLAlchemy Async。实现遵循
[SQLAlchemy 2.0 AsyncIO 官方文档](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
推荐的 `create_async_engine()`、`AsyncEngine.begin()` 事务上下文和显式 `dispose()` 资源
释放方式。

## 5. 方案与解决思路

数据库使用两张表：

```text
runs
  run_id (PK)
  version
  snapshot_json

run_events
  run_id (PK, FK -> runs)
  sequence (PK)
  event_json
```

`create()` 与 `commit()` 在一个 `AsyncEngine.begin()` 事务中同时写快照和事件。`commit()`
使用 `WHERE run_id = ? AND version = expected_version` 的条件更新：影响一行代表成功，零行
则进一步区分不存在和版本冲突。事件序号在事务内读取当前最大值后递增写入。

快照和事件以显式 JSON 保存，而不是使用 Pickle，避免执行不可信反序列化代码，并让数据
可检查、可迁移。Pydantic TypeAdapter 负责 dataclass、枚举、元组和时间类型的转换；损坏
JSON 或 schema 不匹配时转换为明确的持久化依赖错误。

## 6. 实现说明

| 文件 | 职责 | 主要实现 |
| --- | --- | --- |
| [`sqlite.py`](../../src/researchflow/adapters/persistence/sqlite.py) | SQLite RunStore 适配器 | 表定义、异步事务、版本更新、事件追加、JSON 转换和错误翻译 |
| [`persistence/__init__.py`](../../src/researchflow/adapters/persistence/__init__.py) | 适配器公共出口 | 导出 `SQLiteRunStore`，保留内存替身 |
| [`settings.py`](../../src/researchflow/settings.py) | 组合边界配置 | 增加默认数据库 URL 和环境变量读取 |
| [`bootstrap.py`](../../src/researchflow/bootstrap.py) | 生产组合根 | 默认组装 SQLite Store |
| [`pyproject.toml`](../../pyproject.toml) | 运行依赖 | 将 SQLAlchemy Async 和 aiosqlite 纳入默认依赖 |
| [`test_sqlite_run_store.py`](../../tests/adapters/test_sqlite_run_store.py) | 适配器行为测试 | 持久化、事务、并发、冲突、损坏数据库和事件过滤 |
| [`test_app.py`](../../tests/api/test_app.py) | API 集成测试 | 临时 SQLite 隔离及应用重建后查询 |

生产默认数据库位置为 `./data/researchflow.db`，可以用
`RESEARCHFLOW_DATABASE_URL` 显式覆盖。测试数据库全部位于 pytest 的临时目录，不写入
仓库工作区。

## 7. 技术难点与解决方案

### 7.1 快照与事件原子性

- 风险：分开写入会产生状态存在但审计事件缺失，或事件存在但状态没有推进。
- 解决思路：复用 `RunStore.commit()` 端口语义，在单个数据库事务内执行条件更新和事件追加。

### 7.2 JSON 与领域类型往返

- 风险：`RunSnapshot` 包含嵌套 dataclass、枚举、时间和 Mapping，手写转换容易漏字段。
- 解决思路：在适配器边界集中使用 Pydantic TypeAdapter，并对写入失败和存量数据损坏使用
  不同错误语义。

### 7.3 并发版本与事件序号

- 风险：两个写入者都从同一版本和事件序号开始，可能覆盖状态或生成重复序号。
- 解决思路：先以版本条件更新 Run 行；只有赢得版本更新的事务才能追加事件，数据库复合
  主键继续保护 `(run_id, sequence)` 唯一性。

### 7.4 异步连接跨事件循环

- 现象：FastAPI `TestClient` 和直接 `asyncio.run()` 测试可能创建不同事件循环，复用连接池
  中的异步连接会把连接绑定到旧循环。
- 候选方案：要求所有调用共享事件循环；每次测试手工重建全局引擎；禁用连接复用。
- 最终方案：SQLite MVP 使用 SQLAlchemy `NullPool`，每次操作结束即释放连接，并提供
  `close()` 显式 dispose 引擎。
- 选择理由：单机 MVP 更重视生命周期确定性；未来 PostgreSQL 适配器可以使用独立连接池。

### 7.5 损坏数据不能静默覆盖

- 现象：数据库文件或 JSON 快照损坏时，如果初始化逻辑直接重建空库，会丢失故障证据。
- 最终方案：仅对不存在的表执行 `create_all`；SQLite 驱动错误、JSON 校验失败统一转换为
  `DependencyUnavailable`，不删除文件、不回退到内存 Store。
- 选择理由：符合 FR-023 的故障显式化要求，并保留人工恢复的数据现场。

### 7.6 本地构建模块被同名目录遮蔽

- 现象：在仓库根目录运行 `python -m build --wheel` 时，Python 优先导入了仓库已有的
  `build/` 目录，无法找到构建工具的 `__main__`；从父目录执行又确认当前解释器没有安装
  第三方 `build` 包。
- 候选方案：删除或改名现有目录；额外安装 `build`；使用 pip 自带的 PEP 517 Wheel 构建。
- 最终方案：不修改现有目录，也不为一次验证增加运行依赖，改用
  `python -m pip wheel . --no-deps --wheel-dir dist`。
- 结果：成功生成 `researchflow_agent-0.0.0-py3-none-any.whl`。

## 8. 验证与证据

| 检查 | 实际命令 | 结果 | 主要证据 |
| --- | --- | --- | --- |
| SQLite 适配器定向测试 | `python -m pytest -q tests/adapters/test_sqlite_run_store.py -W error::DeprecationWarning` | 7 项通过 | 持久化、事务、并发、冲突、损坏库 |
| 并发稳定性复跑 | 同一并发版本测试连续执行 5 次 | 5 次均通过 | 每次仅一个提交者成功 |
| 全量回归测试 | `python -m pytest -q -W error::DeprecationWarning` | 25 项通过 | [`tests/`](../../tests) |
| 类型检查 | `python -m mypy src/researchflow` | 34 个源文件通过 | [`src/researchflow/`](../../src/researchflow) |
| 静态检查 | `python -m ruff check .` | 通过 | 全仓库 |
| 格式检查 | `python -m ruff format --check .` | 47 个文件符合格式 | 全仓库 |
| 字节码编译 | `python -m compileall -q src tests` | 通过 | 源码与测试 |
| Wheel 构建 | `python -m pip wheel . --no-deps --wheel-dir dist` | 通过 | Wheel 产物位于忽略的 `dist/` 目录 |

全量测试包含一个 API 应用重建场景：第一个应用创建并完成固定 DAG，第二个使用相同 SQLite
文件的新应用实例能够读取完全一致的 Run 快照。该测试证明“重启后可查询”，但没有把
未完成任务自动恢复描述成已实现。

## 9. 当前限制、风险与非承诺

- SQLite 适合单机 MVP，不代表已经支持多节点高并发部署。
- “数据可重新读取”不等于“未完成任务已经自动恢复执行”。
- 首版 schema 尚无迁移版本管理，字段演进需要在后续里程碑加入迁移工具。

## 10. 下一步

1. 完成 SQLite Store 和组合根接入。
2. M2.2 增加启动恢复策略，把无法确认的 `RUNNING` Attempt 转回可重试状态。
3. M2.3 引入后台 Runner、SSE 回放和断线重连。

## 11. 变更历史

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-08-14 | 开发前建立需求、范围、数据方案和风险记录 | ResearchFlow Agent 团队 |
| 2026-08-14 | 完成 SQLite 适配器、组合根接入、测试与验证证据 | ResearchFlow Agent 团队 |

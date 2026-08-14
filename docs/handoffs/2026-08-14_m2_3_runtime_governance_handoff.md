# M2.3 后台执行与实时事件交接

| 项目 | 内容 |
| --- | --- |
| 交接日期 | 2026-08-14 |
| 仓库状态 | M2.2 交付提交，具体版本见 Git 历史 |
| 已完成阶段 | M1 执行闭环、M2.1 SQLite 持久化、M2.2 进程重启安全恢复 |
| 验证基线 | `40 passed`，mypy、Ruff、格式与 compileall 全部通过 |
| 下一阶段 | M2.3：后台执行、领取/租约与 SSE 实时事件 |

## 1. 当前结论

M2.2 已实现：FastAPI 启动时会枚举持久化的 `RUNNING` Run，由 Runtime 原子恢复为 `PAUSED`；运行中的 Task 回到 `READY`，旧 `execution_id` 失效并追加一次 `run.recovered`。重复启动幂等，并发恢复通过乐观锁重判，Resume 使用新执行身份且不重复执行成功 Task。

下一步应先解决当前 HTTP 请求内同步执行的问题，形成进程内后台 Worker 的最小垂直闭环，再让 SSE 订阅同一事件事实来源。不要同时引入 Docker、真实模型、PostgreSQL 或多节点调度。

## 2. 推荐交付顺序

1. 建立 M2.3 开发记录，先冻结提交、领取、租约失效和进程关闭语义。
2. 用失败测试证明 `POST /runs` 不需要等待整个 DAG 完成，后台执行仍通过 Runtime 状态机和 `RunStore.commit()`。
3. 增加最小的单进程 Worker/领取边界；执行身份和租约不能由 FastAPI 或 SQLite 适配器直接修改。
4. 补 Worker 与 Pause、Cancel、服务关闭、超时和迟到结果竞争测试。
5. 基于已有单调事件序号实现 SSE，支持 `after_sequence` 断点续读并保留现有历史 JSON 查询接口。
6. 全量验证并更新开发记录后，再评估 Docker Sandbox 与真实 Capability。

## 3. 接手入口

| 位置 | 关注点 |
| --- | --- |
| [`runtime/service.py`](../../src/researchflow/runtime/service.py) | 当前同步执行循环、执行身份和状态迁移 |
| [`runtime/store.py`](../../src/researchflow/runtime/store.py) | 领取/租约所需的最小消费方端口 |
| [`api/app.py`](../../src/researchflow/api/app.py) | lifespan、非阻塞提交和 SSE 协议边界 |
| [`bootstrap.py`](../../src/researchflow/bootstrap.py) | Worker 生命周期和真实依赖组装 |
| [`tests/runtime/test_service.py`](../../tests/runtime/test_service.py) | 状态、竞争与迟到结果语义 |
| [`tests/api/test_app.py`](../../tests/api/test_app.py) | HTTP 与启动生命周期行为 |
| [`M2.2 实施记录`](../development/2026-08-14_m2_2_process_recovery.md) | 已完成恢复契约与验证证据 |

## 4. 风险与非目标

- 接手前先检查 Git 状态，并保留工作区中的已有改动。
- SQLite 仍是单机 MVP，不把单进程 Worker 声称为分布式调度。
- SSE 只投递已经提交的事件，不建立第二套状态事实来源。
- 不自动 Resume 用户明确暂停的 Run；恢复后的 `PAUSED` 仍需显式 Resume。
- 本阶段不接入 Docker、Git Workspace、真实模型、PostgreSQL 或真实 Artifact Store。

## 5. 接手时第一条指令

> 先检查 M2.2 交付提交和验证记录；基于已通过的启动恢复契约，为 M2.3 建立开发记录和失败测试，优先实现单进程后台 Worker 的非阻塞执行闭环，再接入 SSE，不同时扩展 Docker、真实模型或多节点调度。

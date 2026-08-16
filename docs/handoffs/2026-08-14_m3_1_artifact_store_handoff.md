# M3.1 内容寻址 Artifact Store 交接

| 项目 | 内容 |
| --- | --- |
| 交接日期 | 2026-08-14 |
| 仓库状态 | `main@8d278b2`；M2.4 位于本地未提交工作区 |
| 已完成阶段 | M1、M2.1 SQLite、M2.2 启动恢复、M2.3 后台 Worker 与 SSE、M2.4 Run 内并行 DAG 调度 |
| 验证基线 | `55 passed`，mypy、Ruff、格式、compileall 与 diff check 全部通过 |
| 下一阶段 | M3.1：内容寻址 Artifact Store 与 Runtime 产物闭环 |

## 1. 当前结论

M2 已收口单机运行治理：Run 非阻塞提交，Worker 可并发处理不同 Run，同一 Run 内的
独立 Task 受 `RunBudget.max_concurrency` 限制并行执行；Task 领取、失败传播、暂停、
取消、超时、墙钟预算、迟到结果和重启恢复都落在持久化快照与事件上。

M3 的真实科研执行需要先建立可验证的产物事实来源。仓库已有 `ArtifactRef`、
`ArtifactPayload`、`ArtifactStore` 和 `CapabilityResult.artifacts` 契约，但尚无文件系统
Adapter，Runtime 也尚未保存、验证或向下游传递 Capability 产生的 Artifact。

## 2. 推荐交付顺序

1. 建立 M3.1 开发记录，冻结内容寻址、去重、完整性校验、流式读写和路径安全语义。
2. 以 `ArtifactStore` 为 seam，为文件系统 Adapter 写契约测试：写入、读取、SHA-256、
   同内容去重、不同内容不覆盖、篡改检测和非法标识拒绝。
3. 扩展 Run 快照保存每个 Task 的 `ArtifactRef`，由 Runtime 在 Task 成功提交时原子记录
   产物引用和产物事件。
4. 下游 Capability 只接收已声明依赖且校验有效的上游 Artifact；缺失或损坏时不得把
   Task 判定为可安全复用或进入 Ready。
5. 增加 Artifact 查询/流式下载的最小 HTTP 垂直切片，入口层只负责协议转换。
6. 完成 SQLite 往返、重启后校验和全量验证，再进入 Docker Sandbox 与真实 Capability。

## 3. 接手入口

| 位置 | 关注点 |
| --- | --- |
| [`artifacts/contracts.py`](../../src/researchflow/artifacts/contracts.py) | 已有 `ArtifactStore` 窄接口 |
| [`domain/artifact.py`](../../src/researchflow/domain/artifact.py) | `ArtifactRef` 的哈希、类型和来源字段 |
| [`capabilities/contracts.py`](../../src/researchflow/capabilities/contracts.py) | 请求与结果已经预留 Artifact 引用 |
| [`domain/run.py`](../../src/researchflow/domain/run.py) | 当前快照尚未保存 Task Artifact |
| [`runtime/service.py`](../../src/researchflow/runtime/service.py) | 当前 Task 成功只提交结构化 outputs |
| [`adapters/persistence/`](../../src/researchflow/adapters/persistence/) | SQLite 快照序列化可随领域模型往返 |
| [`M2.4 实施记录`](../development/2026-08-14_m2_4_parallel_scheduler.md) | 已完成调度语义与限制 |

## 4. 风险与非目标

- Artifact URI 必须由 Store 生成，不能直接信任上传文件名或 `artifact_id` 作为路径。
- 大内容必须流式处理，不能为计算哈希一次性全部载入内存。
- 内容变化必须产生新摘要或版本，不能静默覆盖旧内容。
- 本阶段先完成本地文件系统 Adapter，不同时引入 S3、远程对象存储或分布式 GC。
- 不在 Artifact 尚未形成可信闭环时直接接入会执行不可信代码的真实 Coder/Sandbox。
- SQLite 多 Store 同时首次建表存在 bootstrap 竞态；生产迁移治理应作为独立存储任务处理。

## 5. 接手时第一条指令

> 基于已验证的 M2 并行运行时，先以 `ArtifactStore` 公共接口建立失败契约测试，实现安全的
> 本地内容寻址文件存储，再让 Runtime 原子保存并验证 `CapabilityResult.artifacts`；不要
> 同时接入 Docker、真实模型或远程对象存储。

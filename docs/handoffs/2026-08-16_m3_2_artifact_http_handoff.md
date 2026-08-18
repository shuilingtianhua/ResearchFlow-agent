# M3.2 Artifact 查询与流式下载交接

| 项目 | 内容 |
| --- | --- |
| 交接日期 | 2026-08-16 |
| 仓库状态 | `main`；M2.4 已提交为 `5f4e2ee`；M3.1 位于本地未提交工作区 |
| 已完成阶段 | M1、M2.1 SQLite、M2.2 启动恢复、M2.3 后台 Worker 与 SSE、M2.4 Run 内并行 DAG、M3.1 内容寻址 Artifact Store 与 Runtime 产物闭环 |
| 验证基线 | `62 passed`，mypy、Ruff、格式、compileall 与 diff check 全部通过 |
| 下一阶段 | M3.2：Artifact 查询与流式下载 |

## 当前结论

M3.1 已建立本地可信产物闭环：文件系统 Store 按 SHA-256 内容寻址并流式读写，Runtime
在成功提交前校验结果产物，在 Run 快照中保存 `ArtifactRef`，并发出
`artifact.stored` 事件；直接依赖的下游 Capability 只接收已验证引用。组合根通过
`RESEARCHFLOW_ARTIFACT_ROOT` 配置 Store 根目录。

## 推荐交付顺序

1. 增加由 Runtime/ArtifactStore 驱动的最小 Artifact 查询和流式下载 HTTP 端点。
2. 下载前校验 Run、Task 与 ArtifactRef 的存在关系；不得允许任意 URI 或路径读取。
3. 补充鉴权/授权 seam、范围读取或分页策略，并保持大文件不整体载入内存。
4. 完成 API、SQLite 往返和异常映射验证，再进入 ArtifactRef Attempt/创建时间建模与真实 Capability。

## 接手入口

| 位置 | 关注点 |
| --- | --- |
| [`adapters/artifacts/filesystem.py`](../../src/researchflow/adapters/artifacts/filesystem.py) | `ArtifactStore.open()` 流式读取与 URI 校验 |
| [`runtime/service.py`](../../src/researchflow/runtime/service.py) | Task ArtifactRef 的来源和有效性边界 |
| [`api/app.py`](../../src/researchflow/api/app.py) | 新增路由的协议转换位置 |
| [`api/schemas.py`](../../src/researchflow/api/schemas.py) | 当前 RunResponse 已暴露 Task ArtifactRef |
| [`M3.1 实施记录`](../development/2026-08-16_m3_1_artifact_store_runtime.md) | 已验证行为、限制与后续边界 |

## 风险与非目标

- 下载接口必须使用 ArtifactRef 的 Store 解析，不得把 `artifact_id`、URI 或用户输入直接拼成本地路径。
- 本阶段不引入远程对象存储、GC、Docker 或真实模型调用。
- `ArtifactRef` 的 Attempt、创建时间和授权范围字段仍属于后续建模，不要在 HTTP 层临时补字段。

## 接手时第一条指令

> 基于 M3.1 已验证的 `ArtifactStore.open()` 和 Run 快照引用，先写 HTTP 查询/下载失败测试，
> 确保只返回该 Run 已记录且 Store 可读的 Artifact；不要绕过 Runtime 直接读取文件系统。

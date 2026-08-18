# M3.2 Artifact 查询与流式下载实施记录

| 项目 | 内容 |
| --- | --- |
| 状态 | 已实现并验证 |
| 所属里程碑 | `M3.2` |
| 创建日期 | `2026-08-16` |
| 最后验证日期 | `2026-08-16` |
| 关联需求 | `docs/PRD.md` FR-040、FR-043、NFR 10.3 |
| 关联架构 | `docs/ARCHITECTURE.md` API Adapter、Artifact Module |
| 代码版本 | `main` 本地工作区；M3.1 及 M3.2 尚未提交 |

## 1. 需求

为已记录在 Run 快照中的 Artifact 提供最小查询和下载入口：客户端可以查看引用元数据，
并以流式方式读取内容；接口必须限定在指定 Run 的 ArtifactRef 范围内，下载前重新校验
Store 内容，不能把用户输入当作本地路径。

## 2. 业务背景与问题

M3.1 已将产物安全写入文件系统并保存引用，但用户只能通过 Run 快照看到 URI 和摘要，
无法获取实际文件。直接在 API 层拼接 URI 或文件名会绕过 Runtime 的授权和完整性边界，
因此查询/下载必须由 Runtime 解析快照引用并委托 ArtifactStore。

## 3. 范围与非目标

### 本步骤范围

- `GET /runs/{run_id}/artifacts/{artifact_id}` 返回 ArtifactRef 元数据。
- `GET /runs/{run_id}/artifacts/{artifact_id}/content` 使用 `StreamingResponse` 流式返回内容。
- 下载前校验 Run 快照中存在该 Artifact 且 Store 摘要有效；缺失返回 404，Store 未配置或内容损坏返回 503。
- 增加 MIME 类型的安全降级和 API 错误映射。

### 非目标

- 不增加任意 URI、路径或未授权 Artifact 的读取能力。
- 不实现远程对象存储、范围请求、分页、GC、鉴权系统或大文件索引。
- 不在 HTTP 层临时扩展 ArtifactRef 的 Attempt/创建时间字段。

## 4. 当前基线与参考

| 能力 | 当前状态 | 证据或说明 |
| --- | --- | --- |
| Run 快照保存 ArtifactRef | 已实现 | M3.1 `RunSnapshot.task_artifacts` |
| Store 流式读取与摘要校验 | 已实现 | `FilesystemArtifactStore.open/verify` |
| Run 范围元数据查询 | 已实现 | `RuntimeService.get_artifact` |
| 授权后流式下载 | 已实现 | `RuntimeService.open_artifact`、API 路由 |
| 远程存储、鉴权和范围读取 | 规划中 | 后续基础设施切片 |

## 5. 方案与解决思路

API 只做路径参数解析和响应转换。Runtime 先加载 Run 快照，在 `task_artifacts` 中按
`artifact_id` 查找引用，再调用注入的 Store `verify()`；只有校验成功后，下载接口才
返回 Store `open()` 的异步迭代器。元数据响应只复制引用字段，不暴露内部路径。Store
未配置、引用不属于该 Run、文件缺失或摘要不一致分别映射到明确的 503/404 语义。

## 6. 实现说明

| 模块或文件 | 职责 | 主要改动 |
| --- | --- | --- |
| `runtime/contracts.py` | Runtime 公共端口 | 增加 Artifact 查询和流式打开方法 |
| `runtime/service.py` | 访问控制与完整性边界 | 按 Run 快照查找 Artifact、校验 Store、委托流式读取 |
| `api/schemas.py` | 元数据响应 | 增加 `ArtifactResponse` |
| `api/app.py` | HTTP 协议转换 | 增加查询/下载路由、503 错误映射和 MIME 降级 |
| `tests/api/test_artifacts.py` | API 验证 | 覆盖查询、下载、Run 范围、缺失和篡改 |

## 7. 技术难点与解决方案

### 7.1 API 路径与文件系统安全边界

- 现象：Artifact URI 可能包含用户可见字符串，直接将 URI 或 `artifact_id` 拼成本地路径会产生越权或路径穿越风险。
- 最终方案：API 不接触路径；Runtime 只接受指定 Run 快照里的 `ArtifactRef`，Store 再严格校验其内容寻址 URI。
- 选择理由：授权关系和内容完整性继续由消费方 Runtime/Store 所有，入口层保持无业务状态。

### 7.2 大文件响应与完整性校验

- 现象：下载前需要证明内容未丢失或篡改，同时不能将大文件整体读入内存。
- 最终方案：先调用流式摘要 `verify()`，再把 Store `open()` 的异步迭代器交给 `StreamingResponse`。
- 残余风险：当前 `verify()` 后到 `open()` 前仍存在本地文件被替换的 TOCTOU 窗口；后续对象存储/文件句柄方案需继续收窄该窗口。

## 8. 验证与证据

| 检查 | 实际命令 | 结果 | 证据位置 |
| --- | --- | --- | --- |
| Artifact API 定向测试 | `python -m pytest tests/api/test_artifacts.py -q` | 通过，`2 passed` | `tests/api/test_artifacts.py` |
| 全量测试 | `python -m pytest -q -W error::DeprecationWarning` | 通过，`64 passed` | `tests/` |
| 类型检查 | `python -m mypy src/researchflow` | 通过，36 个源文件无问题 | `src/researchflow/` |
| 静态检查 | `python -m ruff check .` | 通过 | 全仓库 |
| 格式检查 | `python -m ruff format --check .` | 通过，65 个文件已格式化 | 全仓库 |
| 字节码编译 | `python -m compileall -q src tests` | 通过 | `src/`、`tests/` |
| Diff 完整性 | `git diff --check` | 通过 | 当前工作区 |

未运行或受限的检查：无。

## 9. 当前限制、风险与非承诺

- 当前接口没有独立鉴权/授权系统，授权范围仅由 Run 快照中的 ArtifactRef 决定。
- 下载接口暂不支持 HTTP Range、分页、断点续传或远程对象存储。
- `verify()` 与 `open()` 之间存在本地文件 TOCTOU 窗口；内容寻址路径能限制误读，但不替代后续句柄级一致性设计。

## 10. 下一步

1. 为 ArtifactRef 补齐 Attempt、创建时间和授权范围字段，并在重启恢复中实现有效产物复用。
2. 增加对象存储或句柄级读取实现，收窄校验与读取之间的一致性窗口。
3. 再进入 Docker Sandbox 与真实 Capability。

## 11. 变更历史

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-08-16 | 完成 Artifact 查询、授权校验和流式下载 API | ResearchFlow Agent 团队 |

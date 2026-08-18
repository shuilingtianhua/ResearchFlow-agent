# M3.1 内容寻址 Artifact Store 与 Runtime 产物闭环实施记录

| 项目 | 内容 |
| --- | --- |
| 状态 | 已实现并验证 |
| 所属里程碑 | `M3.1` |
| 创建日期 | `2026-08-16` |
| 最后验证日期 | `2026-08-16` |
| 关联需求 | `docs/PRD.md` FR-040、FR-042、NFR 10.3 |
| 关联架构 | `docs/ARCHITECTURE.md` Artifact Module、数据与一致性 |
| 代码版本 | `main` 本地工作区，M2.4 基线提交 `5f4e2ee`；M3.1 尚未提交 |

## 1. 需求

为真实科研执行建立可信的产物事实来源：Capability 以异步字节流产生内容，系统按
SHA-256 内容寻址保存、去重和校验；Task 成功时原子保存 `ArtifactRef`，发出结构化
产物事件，并只向声明依赖的下游 Capability 传递已校验引用。

## 2. 业务背景与问题

M2 已经可以持久化 Run 状态和 Task 输出，但结构化输出不能承载大文件、代码、日志或
实验数据，也无法证明下游读取的内容未被篡改。没有可信 Artifact 闭环时，重启恢复和
研究结论追溯都可能把丢失或损坏的内容误判为成功结果。

## 3. 范围与非目标

### 本步骤范围

- 新增本地文件系统 `ArtifactStore`，流式写入和读取内容，按 SHA-256 目录寻址。
- 同内容去重且不使用不可信 `artifact_id` 作为路径；读取和校验拒绝非法 URI、丢失或篡改内容。
- `RunSnapshot` 保存每个 Task 的 `ArtifactRef`，SQLite 可往返持久化。
- Runtime 在成功提交前校验产物，在同一版本提交中记录产物引用和 `artifact.stored` 事件。
- 下游请求只携带直接依赖 Task 的有效 Artifact。

### 非目标

- 不接入 S3、远程对象存储、GC、Docker Sandbox 或真实模型 Capability。
- 不在本步骤增加 Artifact HTTP 下载端点。
- `ArtifactRef` 的 Attempt/创建时间等扩展字段暂留后续 FR-040 完整建模切片。

## 4. 当前基线与参考

| 能力 | 当前状态 | 证据或说明 |
| --- | --- | --- |
| `ArtifactRef`、`ArtifactPayload`、`ArtifactStore` 契约 | 已实现 | `domain/artifact.py`、`artifacts/contracts.py` |
| 内容寻址文件系统存储 | 已实现 | `adapters/artifacts/filesystem.py`、`tests/adapters/test_artifact_store.py` |
| Task 产物快照与事件 | 已实现 | `domain/run.py`、`domain/event.py`、`runtime/service.py` |
| 下游依赖产物校验与传递 | 已实现 | `tests/runtime/test_artifacts.py` |
| 产物查询/流式下载 API | 规划中 | 后续最小 HTTP 垂直切片 |

## 5. 方案与解决思路

Store 先把异步字节流写入随机临时文件，同时计算 SHA-256；写完后将临时文件原子
移动到 `root/<digest 前两位>/<digest>`。已有同摘要文件只删除临时文件，因此同内容
去重且历史内容不会被上传名称覆盖。`open()` 按固定块大小流式读取，`verify()` 重新
计算摘要并严格匹配 Store 生成的 URI。

Runtime 在执行 Attempt 前校验直接依赖的 Artifact；Capability 返回结果后再校验新
产物，随后在一次 `RunStore.commit()` 中更新 Task 状态、结构化输出、ArtifactRef 和
事件。事件只保存引用和摘要，不把大内容写入 SQLite。下游请求的 Artifact 列表来自
显式依赖映射，不读取其他 Task 的隐式产物。

## 6. 实现说明

| 模块或文件 | 职责 | 主要改动 |
| --- | --- | --- |
| `adapters/artifacts/filesystem.py` | 本地 ArtifactStore | 流式写入、SHA-256 内容寻址、去重、完整性和 URI 校验 |
| `domain/run.py` | Run 快照 | 增加 `task_artifacts` 映射 |
| `domain/event.py` | 事件契约 | 增加 `artifact.stored` 事件种类 |
| `runtime/service.py` | 产物可信闭环 | 注入 Store、校验输入/输出产物、原子提交引用和事件、构造下游 Artifact 请求 |
| `api/schemas.py` | HTTP 快照转换 | 暴露 Task Artifact 引用 |
| `adapters/persistence/sqlite.py` | 持久化 | 复用 `RunSnapshot` 类型适配器完成 ArtifactRef JSON 往返，并为停机恢复竞争配置 SQLite 锁等待 |
| `tests/adapters/test_artifact_store.py` | Store 契约验证 | 覆盖写入、读取、去重、篡改、缺失和非法 URI |
| `tests/runtime/test_artifacts.py` | Runtime 验证 | 覆盖成功闭环、下游依赖和不可校验产物失败 |
| `tests/adapters/test_sqlite_run_store.py` | 持久化验证 | 覆盖带 ArtifactRef 的 SQLite 跨实例往返 |

## 7. 技术难点与解决方案

### 7.1 异步流式写入与取消

- 现象：AnyIO 的线程函数包装器不接受目标函数关键字参数，直接传 `mkdir(..., parents=True)` 会在运行时失败；上传中取消还可能留下临时文件。
- 根因：`to_thread.run_sync` 只接收位置参数，且取消传播可能打断文件清理。
- 最终方案：用 `functools.partial` 固化文件操作参数；`put`/`verify` 使用屏蔽取消的有限边界，并在异常路径删除随机临时文件。
- 选择理由：不阻塞事件循环，同时保持已开始的有限 Store 操作完成和临时文件清理。
- 残余风险：当前文件系统不提供跨进程 GC 或配额治理。

### 7.2 产物引用与 Task 状态的原子性

- 现象：如果先保存 ArtifactRef、再更新 Task 状态，进程中断会产生无法解释的半提交状态。
- 最终方案：Store 内容写入完成并验证后，Runtime 在同一 `RunStore.commit()` 中写入 `task_artifacts`、`task.succeeded` 和 `artifact.stored` 事件。
- 选择理由：Run 快照和事件继续由唯一事实来源管理，恢复时不会把未成功提交的引用当作完成结果。

## 8. 验证与证据

| 检查 | 实际命令 | 结果 | 证据位置 |
| --- | --- | --- | --- |
| 全量测试 | `python -m pytest -q -W error::DeprecationWarning` | 通过，`62 passed` | `tests/` |
| 类型检查 | `python -m mypy src` | 通过，36 个源文件无问题 | `src/` |
| 静态检查 | `python -m ruff check src tests` | 通过 | 全仓库 |
| 格式检查 | `python -m ruff format --check src tests` | 通过，41 个文件已格式化 | 全仓库 |
| 字节码编译 | `python -m compileall -q src tests` | 通过 | `src/`、`tests/` |
| Diff 完整性 | `git diff --check` | 通过 | 当前工作区 |

未运行或受限的检查：无。

## 9. 当前限制、风险与非承诺

- 当前组合根使用 `RESEARCHFLOW_ARTIFACT_ROOT` 配置本地 Artifact 根目录；Runtime 的 `artifact_store` 仍可选注入，真实 Capability 接入前还需补充下载 API。
- `ArtifactRef` 当前已有摘要、schema version、生产 Task 和 URI，但 FR-040 要求的 Attempt 与创建时间字段留待后续建模。
- 文件系统 Store 没有远程对象存储、配额、生命周期 GC 或跨主机一致性语义。
- 任务重启恢复目前会重新校验下游输入，但尚未实现“已成功且 Artifact 有效时跳过重执行”的完整复用策略。

## 10. 下一步

1. 配置生产 Artifact 根目录并增加最小查询/流式下载 HTTP 端点。
2. 为 ArtifactRef 补齐 Attempt、创建时间和授权范围，并增加有效产物复用/重启验证。
3. 通过真实 Artifact 闭环后再进入 Docker Sandbox 与真实 Capability。

## 11. 变更历史

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-08-16 | 创建记录并完成文件系统 Store、Runtime 产物闭环与验证 | ResearchFlow Agent 团队 |

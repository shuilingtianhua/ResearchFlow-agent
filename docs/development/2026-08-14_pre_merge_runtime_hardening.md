# 合并前运行时契约加固记录

| 项目 | 内容 |
| --- | --- |
| 状态 | 已实现并验证 |
| 所属里程碑 | M1/M2.1 合并门禁修复 |
| 创建日期 | 2026-08-14 |
| 最后验证日期 | 2026-08-14 |
| 关联需求 | PRD FR-020、FR-021，架构设计第 6、7 节 |
| 触发来源 | Draft PR #1 合并前 Standards/Spec 双轴审查 |
| 代码版本 | `agent/m1-runtime-agents-guide`，本记录随修复提交发布 |

## 1. 需求

在 Draft PR #1 合并到 `main` 前，修复审查发现的三个运行时契约缺口：

1. `TaskSpec.timeout_seconds` 和 `RunBudget.max_wall_seconds` 必须真正限制能力执行。
2. 上游任务不可恢复失败后，其下游任务必须进入 `BLOCKED`，不能混同用户取消。
3. 任务结果提交与暂停/取消竞争产生版本冲突时，必须重新加载状态并记录
   `task.result_ignored`，不能直接把冲突抛给调用方。

## 2. 业务背景与问题

科研任务可能运行很久并调用不受核心控制的能力插件。如果超时配置只存在于数据模型，
插件挂死会让整个 Run 永远停留在运行中。另一方面，`BLOCKED` 与 `CANCELLED` 的业务含义
不同：前者表示依赖失败，后者表示用户或系统主动终止，后续人工恢复策略依赖这一区分。

暂停、取消和异步结果还存在典型的 check-then-commit 竞态：执行者确认身份仍有效后，控制
命令可能抢先提交新版本。旧结果必须在冲突后重新判断，而不是覆盖状态或丢失审计事件。

## 3. 范围与非目标

### 本步骤范围

- 任务级超时和 Run 墙钟预算的最小确定性执行边界。
- 新增 `TaskStatus.BLOCKED`，只标记失败任务的依赖后代。
- 结果成功/失败提交冲突后的有界重新加载与重判。
- 增加对应并发、状态和超时测试。

### 非目标

- 不实现后台 Worker、跨进程租约或服务重启自动恢复。
- 不实现模型 Token 和实验次数的实际计量；当前 Fake Capability 不提供这些用量数据。
- 不增加人工重试 API。

## 4. 方案与解决思路

- 使用 AnyIO `fail_after()` 包围单次 Capability 调用，有效超时取节点超时和 Run 剩余墙钟
  预算的较小值；超时沿用任务失败与受限重试流程并留下原因。
- 在计划 DAG 上计算失败节点的全部后代，失败节点为 `FAILED`、后代为 `BLOCKED`、已经成功
  的任务保持 `SUCCEEDED`，其他未完成的独立分支在 Run 终止时为 `CANCELLED`。
- 成功或失败结果最多重判有限次数。每次提交冲突后重新加载快照：执行身份失效则追加
  `task.result_ignored`，仍有效才重新构建当前状态迁移。

## 5. 技术难点

### 5.1 保持取消传播

AnyIO 超时只转换自身的 `TimeoutError`；外层仍不捕获 `BaseException`，因此调用方取消可以
继续传播，不会被误写成普通任务失败。

### 5.2 复现窄竞态窗口

普通阻塞能力测试只能覆盖“先暂停、后返回”。新增可控 Store 在任务结果提交前插入一次
暂停提交，强制制造版本冲突，验证运行时重新加载并记录迟到结果。

## 6. 实现说明

| 文件 | 职责 | 本次改动 |
| --- | --- | --- |
| [`domain/plan.py`](../../src/researchflow/domain/plan.py) | 任务领域状态 | 增加 `TaskStatus.BLOCKED` |
| [`runtime/service.py`](../../src/researchflow/runtime/service.py) | 执行与控制编排 | 有效超时、下游计算、结果和命令冲突重判 |
| [`test_service.py`](../../tests/runtime/test_service.py) | 运行时行为测试 | 超时、预算、分支阻塞、成功/失败结果竞态和命令冲突 |
| [`README.md`](../../README.md) | 当前能力入口 | 补充运行时可信行为说明 |

超时原因记录在 `task_errors` 和 `task.failed` 事件中，仍遵守节点 `max_attempts`。调用方取消
不经过普通异常分支，继续由 AnyIO/asyncio 取消机制向上传播。

结果提交和暂停、恢复、取消命令最多重新判断 5 次；执行循环中的 Ready、Started、Complete
提交如果与控制命令冲突，也会重新加载最新状态，而不是基于旧快照继续运行。

## 7. 验证与证据

| 检查 | 实际命令 | 结果 | 证据 |
| --- | --- | --- | --- |
| 运行时定向测试 | `python -m pytest -q tests/runtime/test_service.py -W error::DeprecationWarning` | 12 项通过 | [`test_service.py`](../../tests/runtime/test_service.py) |
| 全量回归 | `python -m pytest -q -W error::DeprecationWarning` | 31 项通过 | [`tests/`](../../tests) |
| 类型检查 | `python -m mypy src/researchflow` | 34 个源文件通过 | [`src/researchflow/`](../../src/researchflow) |
| 静态检查 | `python -m ruff check .` | 通过 | 全仓库 |
| 格式检查 | `python -m ruff format --check .` | 48 个文件符合格式 | 全仓库 |
| 字节码编译 | `python -m compileall -q src tests` | 通过 | 源码与测试 |

新增测试使用可控 Store 在提交前插入版本更新，确定性覆盖原先很窄的 check-then-commit
竞态窗口；成功结果和失败结果分别验证，避免只修一条路径。

## 8. 当前限制与下一步

- 本步骤只执行墙钟预算；模型 Token、实验次数和并发额度需要真实能力回传用量后实现。
- 合并前必须重新执行双轴审查，并确认 GitHub 分支规则和 PR 状态。

## 9. 变更历史

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-08-14 | 根据合并前审查建立修复记录 | ResearchFlow Agent 团队 |
| 2026-08-14 | 完成三个阻塞项修复及命令冲突补强，31 项测试通过 | ResearchFlow Agent 团队 |

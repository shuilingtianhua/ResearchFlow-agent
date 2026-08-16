# ResearchFlow Agent

ResearchFlow Agent 是一个基于 Sea-mult-agent 业务基线、使用 Python 重构的多智能体科研执行系统。它面向论文理解、代码仓库分析、受控实验、Scientific AutoResearch 与证据化报告生成。

当前已经完成 M1 可运行执行闭环和 M2 持久化与运行治理：提交研究目标后，系统会生成固定任务图，把状态和事件原子写入 SQLite，再由后台 Worker 在 `RunBudget.max_concurrency` 限制内并行执行无依赖冲突的 Fake Capability。服务重启时会把中断执行恢复为可安全 Resume 的暂停状态，客户端可通过 SSE 按事件序号实时续读。

## 文档

- [产品需求文档](docs/PRD.md)
- [Python 架构设计](docs/ARCHITECTURE.md)
- [开发记录规范与索引](docs/development/README.md)
- [当前项目交接与下一步](docs/handoffs/README.md)
- [M1 执行闭环实施记录](docs/development/2026-08-14_m1_execution_loop.md)
- [M2.1 SQLite 持久化实施记录](docs/development/2026-08-14_m2_sqlite_persistence.md)
- [M2.2 进程重启安全恢复实施记录](docs/development/2026-08-14_m2_2_process_recovery.md)
- [M2.3 后台执行与 SSE 实施记录](docs/development/2026-08-14_m2_3_background_worker_sse.md)
- [M2.4 Run 内并行 DAG 调度实施记录](docs/development/2026-08-14_m2_4_parallel_scheduler.md)

## 本地启动

```powershell
python -m pip install -e ".[dev]"
python -m researchflow
```

默认健康检查地址为 `http://127.0.0.1:8000/health`。

默认数据库为 `./data/researchflow.db`。可以通过环境变量覆盖：

```powershell
$env:RESEARCHFLOW_DATABASE_URL = "sqlite+aiosqlite:///D:/data/researchflow.db"
python -m researchflow
```

运行时会执行节点超时和 Run 墙钟预算；上游失败的依赖后代标记为 `blocked`；暂停或取消与
任务结果竞争时，旧结果不会覆盖新状态，并会留下 `task.result_ignored` 事件。

## 当前 API

```text
POST /runs
GET  /runs/{run_id}
GET  /runs/{run_id}/events?after_sequence=0
GET  /runs/{run_id}/events/stream?after_sequence=0
POST /runs/{run_id}/pause
POST /runs/{run_id}/resume
POST /runs/{run_id}/cancel
```

提交示例：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/runs `
  -ContentType 'application/json' `
  -Body '{"goal":"复现论文并生成实验报告"}'
```

## 设计基线

- Sea-mult-agent：业务流程、运行状态、事件、预算、租约和 AutoResearch 可信边界。
- AGI-saber：Python 工程组织、FastAPI 启动、基础设施适配器和测试习惯。
- ResearchFlow Agent：保留业务语义，但采用独立的 Python 模块边界和类型化契约。

参考项目仅作为设计输入；后续若复用代码或素材，将单独核对许可证并保留来源说明。

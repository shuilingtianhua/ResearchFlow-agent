# ResearchFlow Agent

ResearchFlow Agent 是一个基于 Sea-mult-agent 业务基线、使用 Python 重构的多智能体科研执行系统。它面向论文理解、代码仓库分析、受控实验、Scientific AutoResearch 与证据化报告生成。

当前已经完成 M1 可运行执行闭环和 M2.1 SQLite 持久化：提交研究目标后，系统会生成固定任务图，通过 Fake Capability 执行，并把完整状态、输出和事件原子写入 SQLite。服务重启后可以重新查询已有 Run；未完成任务的自动恢复、真实模型和 Docker Sandbox 将在后续里程碑接入。

## 文档

- [产品需求文档](docs/PRD.md)
- [Python 架构设计](docs/ARCHITECTURE.md)
- [开发记录规范与索引](docs/development/README.md)
- [M1 执行闭环实施记录](docs/development/2026-08-14_m1_execution_loop.md)
- [M2.1 SQLite 持久化实施记录](docs/development/2026-08-14_m2_sqlite_persistence.md)

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

## 当前 API

```text
POST /runs
GET  /runs/{run_id}
GET  /runs/{run_id}/events?after_sequence=0
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

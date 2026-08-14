# ResearchFlow Agent

ResearchFlow Agent 是一个基于 Sea-mult-agent 业务基线、使用 Python 重构的多智能体科研执行系统。它面向论文理解、代码仓库分析、受控实验、Scientific AutoResearch 与证据化报告生成。

当前阶段已经冻结产品需求和 Python 代码框架，业务能力将按照垂直切片逐步实现。

## 文档

- [产品需求文档](docs/PRD.md)
- [Python 架构设计](docs/ARCHITECTURE.md)

## 本地启动

```powershell
python -m pip install -e ".[dev]"
python -m researchflow
```

默认健康检查地址为 `http://127.0.0.1:8000/health`。

## 设计基线

- Sea-mult-agent：业务流程、运行状态、事件、预算、租约和 AutoResearch 可信边界。
- AGI-saber：Python 工程组织、FastAPI 启动、基础设施适配器和测试习惯。
- ResearchFlow Agent：保留业务语义，但采用独立的 Python 模块边界和类型化契约。

参考项目仅作为设计输入；后续若复用代码或素材，将单独核对许可证并保留来源说明。

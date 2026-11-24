# Moumou Agent

[English](README.en.md)

Moumou Agent 是一个 Python 多代理协作系统。你提供任务和交付要求，主代理分配工作，子代理在各自的工作区内执行。框架根据依赖安排并行任务，记录人工澄清和审批，并用正式交付版本进行验收。

## 快速使用

需要 Python 3.11 或更新版本。激活 Python 环境后，在仓库根目录执行：

```bash
python -m pip install -e .
```

把 `.env.example` 复制为 `.env`，填写 `DEEPSEEK_API_KEY_OFFICIAL`。默认模型为 DeepSeek 官方 `deepseek-flash`。使用 HKGAI 时填写对应密钥，并传入 `--llm-profile hkgai`。联网搜索为可选功能。

按[使用指南](docs/SETUP.md)创建自己的 Markdown 任务目录。以下命令把本次结果写入一个新的运行目录：

```bash
python -m moumou --tasks runs/my-input --plan
python -m moumou --tasks runs/my-input --human console --out runs/my-run
```

在终端回复澄清问题，按请求决定是否授权和接受结果。完成后，从 `runs/my-run/report.md` 查看状态和交付清单。框架在运行目录内自动建立各代理的工作区。

## 系统组成

| 模块 | 主要职责 |
| --- | --- |
| 编排 | 主代理提议分工，调度器按依赖运行子任务，处理阻塞和恢复 |
| 上下文与记忆 | 保留确认规则、当前输入版本和近期结果，按容量整理历史 |
| 工具与工作区 | 按角色限制文件访问，通过人工请求处理需要授权的操作 |
| 版本与验收 | 保存已声明的交付版本，核对依赖、测试结果和最终文件 |
| 模型与记录 | 调用模型和搜索服务，记录用量、人工决定及运行证据 |

[使用指南](docs/SETUP.md) · [技术文档](docs/ARCHITECTURE.md) · [更新日志](CHANGELOG.md)

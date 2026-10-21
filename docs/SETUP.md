# 环境配置

> 从零到能跑测试、能连上模型，大约 5 分钟。

## 1. 系统要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | macOS / Linux | Windows 建议用 WSL2 |
| Python | **3.11 及以上** | 当前开发环境实测 3.14.6 |
| 包管理 | [uv](https://docs.astral.sh/uv/)（推荐） | 也可以只用 `venv` + `pip` |
| Git | 任意近期版本 | |
| 网络 | 能访问 `api.deepseek.com` | 只跑测试和 demo 则不需要 |
| Docker | 可选 | 只有 P1 的沙箱后端才需要 |

依赖版本由 `pyproject.toml` 约束，核心是：`pydantic>=2.7`、`httpx>=0.27`、`pyyaml>=6.0`、
`pytest>=8`、`ruff>=0.6`。

## 2. 安装

```bash
git clone https://github.com/Greensheep04/moumou-Agent.git
cd moumou-Agent
make install
```

`make install` 做两件事：在仓库根目录创建 `.venv`，然后以可编辑模式安装本项目和开发依赖。

如果机器上没有 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

或者完全不用 uv：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`make install` 只在 `.venv` 不存在时创建，重复执行是安全的。

## 3. 配置模型

```bash
cp .env.example .env
```

然后编辑 `.env`，填上自己的 key：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | **是** | 无 | 在 <https://platform.deepseek.com/api_keys> 申请 |
| `DEEPSEEK_MODEL` | 否 | `deepseek-flash` | DeepSeek-V4.1-Flash-1017 的稳定 ID |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | 一般不用改 |
| `DEEPSEEK_TIMEOUT_S` | 否 | `180` | 单次请求超时，长任务可调大 |

几点说明：

- `.env` 已在 `.gitignore` 中，**不会**被提交
- 每人用自己的 key，便于分别观察额度和排查问题
- 环境变量优先级高于 `.env`，CI 里可以直接注入

## 4. 验证

分两步，第一步不需要 key：

```bash
make test
```

期望结尾：`65 passed`。

第二步需要 key：

```bash
make smoke
```

它会依次做三件事：普通对话、带 function calling 的请求、列出框架注册的工具。
正常输出：

```
base_url : https://api.deepseek.com
model    : deepseek-flash

[1] plain chat  1.57s
    content   : 'pong'
    tokens    : 38 in / 14 out

[2] tool call   1.13s
    tool      : echo  args={'text': 'hi'}

[3] agent advertises 8 tools: ['ask_human', 'delete_file', 'echo', 'read_file',
                              'run_python', 'shell', 'submit_plan', 'write_file']

OK — the API path is wired correctly.
```

如果第 2 项没有返回 tool call，说明模型的函数调用没生效，拆解功能会受影响，需要优先排查。

## 5. 常用命令

| 命令 | 作用 | 需要 key |
| --- | --- | --- |
| `make install` | 建环境、装依赖 | 否 |
| `make test` | 跑全部测试 | 否 |
| `make lint` | 代码检查 | 否 |
| `make fmt` | 自动修复可修复的问题 | 否 |
| `make demo` | 端到端演示（假模型，秒级） | 否 |
| `make smoke` | 验证 key 与模型名 | **是** |
| `make eval` | 跑评测集 | **是** |
| `make clean` | 清理运行产物 | 否 |

所有命令都在**仓库根目录**执行，且都走 `.venv/bin/python`，不会污染系统环境。

## 6. 各模块的额外依赖

| 模块 | 额外需要 |
| --- | --- |
| P1 执行层 | Docker（可选；本地沙箱不需要） |
| P2 交互层 | 一个支持 ANSI 的终端（做 TUI 用） |
| P3 评测层 | 无 |

## 7. 目录与产物约定

| 路径 | 说明 | 进版本控制 |
| --- | --- | --- |
| `.venv/` | 虚拟环境 | 否 |
| `.env` | 本地密钥 | **否** |
| `runs/` | 运行时产物：数据库、工作区、执行日志、评测报告 | 否 |
| `eval/tasks/` | 评测题库 | 是 |
| `docs/` | 文档 | 是 |

`make clean` 会删除 `runs/`、`.pytest_cache`、`.ruff_cache` 和所有 `__pycache__`。
如果 `runs/` 里有想保留的评测报告，先拷出来再执行。

## 8. 遇到问题

常见问题集中在 [FAQ](FAQ.md)：找不到模块、key 不生效、git 推送失败、测试跑的是旧代码等。
新增的坑建议随手补进 FAQ，避免下一个人再踩一遍。

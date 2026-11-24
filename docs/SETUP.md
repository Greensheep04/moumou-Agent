# Moumou Agent 命令行使用指南

[English](SETUP.en.md) · [技术文档](ARCHITECTURE.md)

## 1. 安装与配置

需要 Python 3.11 或更新版本。以下命令在仓库根目录执行。创建并激活 Python 环境后安装项目。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1` 激活环境，用 `Copy-Item .env.example .env` 复制配置。已有 Python 环境时可直接激活该环境。

打开 `.env`。使用默认的 DeepSeek 官方 API 时，只需填写 `DEEPSEEK_API_KEY_OFFICIAL`。使用 HKGAI 时填写 `DEEPSEEK_API_KEY`，按服务提供的信息设置网关模型与地址，并在运行时加入 `--llm-profile hkgai`。实际密钥留在本机。需要联网搜索时，再为 Tavily 或 SerpApi 设置对应的供应商和密钥。

## 2. 写下自己的任务

命令行通过 `--tasks` 接收一个 Markdown 目录。创建 `runs/my-input/my-project/`，并按下方格式写入三个文件。团队描述和任务内容可以用中文或英文。

**`runs/my-input/README.md`**

```markdown
# My project

<!-- evaluation-bank: supplementary -->

- [Team](my-project/01_role_initialization.md)
- [Task 1](my-project/02_task_1.md)
```

**`runs/my-input/my-project/01_role_initialization.md`**

````markdown
# Scenario 1: My project

| ID | Role |
| --- | --- |
| `lead` | Lead |
| `builder` | Builder |
| `reviewer` | Reviewer |
| `publisher` | Publisher |

## Lead (lead)

```text
向用户确认要求，安排工作并汇总交付。
```

## Builder (builder)

```text
根据要求生成产物并说明使用方式。
```

## Reviewer (reviewer)

```text
独立检查提交的产物，记录看到的问题和验证结果。
```

## Publisher (publisher)

```text
根据已检查的版本整理最终发布材料。
```
````

**`runs/my-input/my-project/02_task_1.md`**

````markdown
# Task 1

```text
请制作一份 Markdown 项目启动清单，交付为 kickoff.md。
按准备、执行和检查三个阶段组织。为每个阶段写明要做的事和完成标准。
如果需要确认项目类型或使用人群，请先问我。
```
````

把最后一个文件里的内容换成自己的任务。团队文件需要四个角色，第一位是主代理。角色表的名称和顺序应与下面四段角色定义一致。需要同一团队继续执行第二题时，加入 `03_task_2.md`，在输入目录的 README 中链接它。第二题使用同样的单个 `text` 代码块。

## 3. 从一个终端开始运行

```bash
python -m moumou --tasks runs/my-input --scenario scenario_1 --plan
python -m moumou --tasks runs/my-input --scenario scenario_1 --human console --out runs/my-run
```

`--plan` 检查输入格式、选中的角色、模型配置和预算，不调用模型。第二条命令启动任务。使用 `--out` 指定新的目录。框架会创建该目录及各角色的工作区。若目录已存在，请为新运行换一个名称。`--out` 指向运行结果的位置，`--tasks` 指向任务定义。

遇到 `Your answer:` 时，先查看请求的内容和类型。

| 请求 | 在终端输入 |
| --- | --- |
| 需求澄清 | 直接输入答复。有选项时，使用显示的选项 |
| 操作审批 | 输入 `approve` 或 `reject`，后面可加说明 |
| 结果审阅 | 查看交付文件后，输入 `approve` 或 `reject`，后面可加说明 |

需要显式审批每次 Shell、Python 和文件删除时，加 `--approval-mode manual`。默认的 `auto` 根据可用环境选择命令审批方式。需要调整用时，可用 `--scenario-timeout` 或 `--task-timeout`，单位为秒，后者控制单个节点。所有选项可用 `python -m moumou --help` 查看。

## 4. 在第二个终端回答

希望任务与人工交互分开时，安装可选界面依赖，并用两个终端连接同一个运行目录。

```bash
python -m pip install -e '.[p2]'
# 终端一
python -m moumou --tasks runs/my-input --scenario scenario_1 --human files --out runs/my-file-run
# 终端二
python -m moumou.ui.cli --run runs/my-file-run
```

用 `/pending` 查看请求 ID。用 `/answer ID 答复` 回复问题，用 `/approve ID` 或 `/reject ID` 做决定。`/history` 查看已记录的交互。`/quit` 关闭交互界面，后台运行继续。

## 5. 找到答复、产物和工作区

- **运行状态**打开 `runs/my-run/report.md`。结构化汇总位于 `test.json`。
- **文字答复**查看 `runs/my-run/scenario_1/task_1-result.json` 中的根任务结果。第二题对应 `task_2-result.json`。
- **交付文件**打开 `runs/my-run/scenario_1/task_1-artifacts/manifest.json`。在 `entries` 中按原始 `path` 找文件，`file` 是归档副本相对于场景目录的路径。审阅时使用这份快照。
- **角色工作区**在对应的任务结果中查看 `workspaces`。其中为每个角色记录实际目录。每次运行会建立新的目录，同一场景的下一题通过正式交接接收上一题的成果。
- **交互记录**场景目录中的 `dialogue.jsonl` 保存请求、回复及工具交互，`human/` 保存人工决定。运行记录可能包含你的任务材料和答复。

已有运行的报告可以离线重建。

```bash
python -m moumou.eval.report --run runs/my-run
```

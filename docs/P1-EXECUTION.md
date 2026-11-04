# P1 执行与工具层：使用与交付说明

## 当前交付范围

P1 提供文件工具、shell/Python 执行、Git 本地操作、经过框架审批的推送、Tavily 搜索，以及可注入 Engine 的 Git worktree Provider。共享 contracts 和 P0 编排代码保持不变。

所有工具通过 `default_tools()` 注册。工作区 Provider 通过构造参数注入，默认 Engine 仍保留原来的 LocalDirs，避免改变队友的既有行为。

## 环境和运行

本机 checkout：`D:\Projects\moumou-Agent`。专用环境：`D:\Dev\conda-envs\moumou-agent`。环境规格位于 `environment.yml`，Python 包锁位于 `requirements-win-py311.lock`。规格文件不包含机器绝对路径。

在本机 PowerShell 中，从项目目录执行：

```powershell
conda activate D:\Dev\conda-envs\moumou-agent
python -m pip install -r requirements-win-py311.lock
python -m pip install --no-deps -e .
python -m pytest -q
python -m ruff check src tests
python -m moumou.demo.p1
```

如需从零创建环境，先指定 Conda 缓存，再通过 `conda env create --prefix ... --file environment.yml` 创建。不要运行仓库旧 Makefile 的 `make install`，它会额外创建 .venv。代码、运行命令和测试均应使用上述专用环境。

P1 demo 不需要 LLM 或搜索密钥。每次在 `runs/p1-demo/<run-id>/` 生成独立运行目录，包括数据库、trace、工作区和 `p1-summary.json`，不删除此前演示记录。

## 接入 Engine

```python
from pathlib import Path

from moumou.adapters import GitWorkspaces
from moumou.orchestrator import Engine, Settings

settings = Settings(root=Path("runs/my-run"))
provider = GitWorkspaces(settings.root / "workspaces")

# llm、human 使用团队已有实现；不要在工具内部实现审批。
engine = Engine(llm, human=human, workspace=provider, settings=settings)
```

如需预装项目代码，使用 `GitWorkspaces(root, seed=trusted_repo_path)`。seed 必须是可信本地 Git 仓库，每个 project/conversation 克隆独立的管理仓库。

## 工具参数

| 名称 | 参数 | 说明 |
| --- | --- | --- |
| read_file | path | UTF-8 读取，最多 8192 字节正文 |
| list_files | path，默认 . | 递归返回相对文件路径的 JSON 数组 |
| search_files | query；path 默认 . | 按字面文本搜索，返回 path/line/text；跳过超过 1 MiB 的文件 |
| write_file | path、content；mode=overwrite/append | 创建、覆盖或追加，返回产物路径 |
| delete_file | path；recursive=false | 目录删除必须显式 recursive=true，返回删除的文件路径 |
| apply_patch | path、old、new | old 必须恰好出现一次；文件上限 1 MiB |
| shell | command；timeout_s 默认 60 | 使用所在平台 shell；工具内部超时范围 (0,110] 秒 |
| run_python | path 或 code 二选一；args；timeout_s | 使用运行框架的 Python 解释器；args 推荐字符串数组 |
| git | operation；paths/message/name/branch | status/diff/add/commit/log/branch/merge |
| git_push | branch；remote 默认 origin | 只接受已配置 remote 名和本地分支，不提供 force push |
| web_search | query；max_results 默认 5，范围 1–5 | Tavily 检索，返回 title/url/snippet JSON |

保留 echo、ask_human、submit_plan、submit_result 和 submit_revision。后四种标记工具由编排器拦截，不应当作普通执行工具直接调用。

示例：

```python
from moumou.contracts import ToolContext
from moumou.tools import default_tools

tools = {tool.name: tool for tool in default_tools()}
ctx = ToolContext(agent_id="coder@t1", task_id="t1", workspace=workspace)
result = await tools["run_python"].run(
    {"code": "open('result.txt', 'w').write('ok'); print('done')"}, ctx
)
```

普通失败使用 `ok=False` 和简短 error；取消在清理后重新抛出 `CancelledError`。结果不包含内部 traceback。新建、修改及删除文件通过相对路径的 artifacts 传递；shell/Python 使用执行前后内容指纹识别变化。

## 输出与进程生命周期

stdout/stderr 同时持续读取；每路最多保留 8192 字节正文，超出数据继续排空并丢弃，避免管道堵塞。结果增加 `[truncated]` 标记并设置 `truncated=True`。JSON 工具保持 JSON 可解析，通过减少结果条数和字段限额控制大小。

Windows 进程先以 CREATE_SUSPENDED 创建，加入 kill-on-close Job Object 后恢复主线程；POSIX 使用独立进程组。超时、外层取消以及命令正常完成后的后台后代均进入清理流程。通用兼容入口 `run_process()` 保留给 P0 验收使用，其有限正超时不受工具 110 秒上限限制。

已有 P0 验收使用 `python3 ...`；兼容入口将开头的 python3 映射到当前项目解释器，其余 shell 语法保持平台原样。Windows shell 命令应使用 Windows 引号语法，不能把任意 POSIX 脚本直接当作跨平台命令。

## Git worktree 和产物整合

每个 project/conversation 拥有自己的管理仓库；每个 Agent 身份映射到稳定哈希路径和独立分支。身份中含斜杠、问号等字符不会因简单字符替换而碰撞。

`integrate(src, dst, artifacts)` 的行为：

1. 校验两个工作区属于本 Provider、同一管理仓库，且产物路径合法。
2. 目标工作区必须干净；如有本地修改，返回错误，要求先提交或解决。
3. 将声明的文件内容和删除信息构造成交付提交，不直接合并源分支的全部修改。
4. 每个文件使用上次向目标交付的版本作为三方基线；首次交付则使用源工作区曾接收的该文件版本。即使 Agent 已提交自己的修改，也不会把自己的提交误当作输入基线。
5. 在临时 Git index 中仅对声明路径应用三方补丁。冲突时丢弃临时 index 并返回 `ok=False` 和 conflicts，目标工作区保持原样。
6. 成功时生成目标合并提交、物化验证后的文件树，并原子更新交付与输入版本引用；同一 Provider 实例通过锁串行整合。

因此，源工作区的未提交产物也可以交付；未声明的私有文件不会随源分支一起进入目标。Git 工具仍可显式提交源分支，用于展示开发历史。

release 只移除受本 Provider 管理的干净 worktree，保留分支与交付历史；脏工作区不强制删除。不要删除管理根目录后继续使用旧 Provider。

## 审批和搜索配置

审批由现有 Gate 执行：写文件、Git 本地操作和联网默认免审批；删除、shell/Python、推送默认需审批。应用不能直接调用工具后声称已经执行审批，实际用户操作应经过 Gate。

Tavily 密钥通过 `TAVILY_API_KEY` 环境变量或 `ToolContext.env` 注入；密钥缺失时返回配置错误。支持构造时传入 api_key/HTTP transport，便于服务装配和测试，不在模型参数中索取密钥。

搜索遵循 [Tavily 官方 Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)：POST /search，Bearer 授权，将结果 content 映射为 snippet。HTTP 请求超时 20 秒，响应上限 2 MiB；401、429、服务错误和超时返回可读错误，不回显服务原始响应体。

Git 本地操作和推送会移除继承/上下文中的 GIT_* 重定向及配置变量，保留系统凭证管理器和 SSH 默认配置；不支持通过 GIT_SSH_COMMAND 注入自定义命令。GitHub 推送使用系统已有 Git 凭证管理器或 SSH 配置，不在工具参数里传 Token；无交互凭证时返回失败，不挂起等待终端输入。

## 明确的边界与待确认事项

- LocalJail 是应用层路径校验，不是内核或容器沙箱；它拒绝文件工具越界及 Git 管理入口访问，不能阻止批准的任意 shell/Python 代码访问其他系统资源。
- Git worktree 共享同一管理仓库的 Git 元数据，不能作为恶意代码之间的安全隔离。Docker 后端尚未实现。
- 工作区协议仍是同步方法，Git 管理操作会占用当前事件循环，每个 Git 子命令时限 30 秒。大仓库需要 P0 协调异步协议后优化。
- 锁覆盖同一 Provider 实例的并发调用，不提供多进程共享 Provider 的事务保证。
- 执行前后指纹比较不能报告内容未变化的写入，也不能保留在进程运行期间创建后又删除的临时产物；这些不属于最终交付文件。
- 搜索属于外部付费服务，需要团队提供有效密钥；本地测试不消耗真实检索额度。
- 已做 Windows 本地验证。Windows/Linux CI 已配置，但运行结果以实际 GitHub Actions 为准，不能将配置文件视为 Linux 验证证据。
- 与 P0/P3 的人工联调、真实模型测试及正式课程测试集尚需团队完成；本实现已提供不依赖模型的 Engine 集成测试。

## 演示与验收

`python -m moumou.demo.p1` 应展示 3 个任务完成、0 个失败，输出 hello, moumou；Git 图中有交付合并提交；冲突列表包含 same.txt，且回滚后原内容保留。

对应测试：
- test_filesystem_tools.py：文件操作、路径越界、junction、保护目录、字节截断。
- test_process_runner.py：环境、解释器、产物、超时/取消进程树、双路输出。
- test_git_tools.py / test_git_workspace.py：真实 Git 和 worktree，重复交付、删除、冲突、并发。
- test_git_push.py：本地 bare remote，真实 Gate 批准/拒绝。
- test_web_search.py：模拟 HTTP 协议、错误、超时、结构化限额。
- test_p1_integration.py：既有 Engine 的并行交付、依赖传递和脚本执行。

验证结果及审查修复记录见 `docs/P1-PROGRESS.md`。持续提交可用 `git log --graph --oneline origin/main..HEAD` 展示；正式报告中的提交历史截图应在最终分支与团队贡献记录确定后补充。


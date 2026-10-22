# 现在该做什么

[简体中文](TASKS.md) · [English](TASKS.en.md)

> 给三个人的一页说明。细节在各自的详细任务书里，这里只说**从哪开始**。

## 一、先跑起来（10 分钟）

```bash
git clone https://github.com/Greensheep04/moumou-Agent.git
cd moumou-Agent
make install
cp .env.example .env      # 填自己的 DeepSeek key
make test                 # 142 个测试，不需要 key
```

## 二、你负责什么

| 你 | 第一个 PR 应该是 | 验收命令 |
| --- | --- | --- |
| **P1 执行层** | 把路径检查抽成独立的 `LocalJail`，再补 `git` / `git_push` / `web_search` 三个工具 | `pytest tests/test_tool_contract.py -q` |
| **P2 交互层** | 一个持久化的 `Channel`（SQLite 或 JSONL），把契约测试里的 `make_bus` 指向它 | `pytest tests/test_channel_contract.py -q` |
| **P3 数据层** | 把 `trace.jsonl` 导进 SQLite，让 `make eval` 能跑起来 | `make eval` |

接口、字段和行为约定看 [接口说明](../INTERFACES.md)，不用自己猜。

## 三、最近变了什么

子代理这块最近改动比较多，如果你手上有旧文档会对不上：

| 变化 | 影响你吗 |
| --- | --- |
| 主代理有了独立提示词 | 不影响 |
| 子代理交活改成 `submit_result`（结构化字段） | 不影响 |
| 子任务失败会唤醒父任务；重派的子任务 id 带轮次前缀 | 不影响 |
| 角色可以被评审修改，带版本号 | 如果读 `roles` 表，注意 `version` / `updated_at` |
| **`TaskNode.result` 现在是结构体，不再是字符串** | **如果读任务结果，要按字段取** |

细节见 [子代理机制](../SUBAGENTS.md)。

## 四、测试怎么跑

| 命令 | 耗时 | 说明 |
| --- | --- | --- |
| `make test` | ~2 秒 | 全部 142 个 |
| `pytest tests/test_tool_contract.py -q` | ~1 秒 | P1 的验收 |
| `pytest tests/test_channel_contract.py -q` | <1 秒 | P2 的验收 |
| `make eval` | 每题几秒 | 需要 key，P3 的验收 |

**契约测试是"换了实现也必须全绿"的。** 要改 `src/moumou/contracts/` 之前，先在群里说一声。

## 五、开分支

一人一条长期分支：`feat/p1-sandbox`、`feat/p2-cli`、`feat/p3-eval`。
合并前先 rebase 到 main，保持历史线性。

## 六、卡住了怎么办

先看 [常见问题](../FAQ.md)。还是没有答案，把你的分支和复现步骤发出来，别自己憋着——
契约层是共享的，你憋出来的绕过方案以后要还。

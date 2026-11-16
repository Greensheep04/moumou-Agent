# P2 —— 交互与通信层 交付文档

> 分支：`feat/p2-cli`
> 范围：消息总线（持久化 Pub-Sub）· 人类成员代理 · CLI 交互界面 · Trace 回放

---

## 一、代码清单

| 模块 | 路径 | 说明 |
| --- | --- | --- |
| 消息总线 | `src/moumou/bus/__init__.py`、`src/moumou/bus/sqlite.py` | `SqliteBus`：`Channel` 端口的持久化实现 |
| 人类代理 | `src/moumou/agents/__init__.py`、`src/moumou/agents/human.py` | `CliHuman`（可取消的 `HumanIO`）+ `HumanAgent` |
| 终端界面 | `src/moumou/ui/__init__.py`、`src/moumou/ui/cli.py` | 基于 Rich 的 CLI，展示群聊/私聊/通知/审批 |
| Trace 回放 | `src/moumou/ui/replay.py` | 从 `runs/<run>/trace.jsonl` 渲染时间轴与 span 树 |
| 测试 | `tests/test_p2.py` | 持久化恢复、DM 顺序无关、慢订阅者、`ask` 取消 |

### 契约测试接入

`tests/test_channel_contract.py` 中的 `make_bus` 夹具已由内存版切换到 P2 持久化实现：

```python
@pytest.fixture
def make_bus():
    return lambda: SqliteBus(db_path=Path(tempfile.mkdtemp()) / "bus.db")
```

---

## 二、实现要点（对齐六条铁律）

1. **先落盘再投递**：`SqliteBus.publish` 先 `INSERT` 到 SQLite（`isolation_level=None`
   自动提交），再向订阅者投递；进程重启后 `history()` 仍可读回。
2. **回调不阻塞总线**：每个订阅回调放进独立 `asyncio` Task，`asyncio.wait_for`
   超时控制；慢订阅者超时丢弃并记 `warning`，不拖死总线。
3. **路由与作用域隔离**：DM 只投给 `recipients`；广播（`recipients=[]`）投给所有订阅者；
   `history` 按 `project_id + conversation_id` 过滤、按成员集合交集判断（顺序无关）、
   严格按时间升序返回。
4. **`ask` 可取消**：`CliHuman.ask` 用 `asyncio.Future` 挂起，捕获 `CancelledError`
   后清理自身 future 再重抛，不留脏状态。
5. **UI 退出不留孤儿 Task**：`run_cli` 在 `finally` 中取消后台 demo Task 并 `reject_all()`
   清空挂起审批。
6. **UI 不做业务判断**：界面只渲染问题、把输入包成 `HumanResponse` 传回，不写任何
   “自动批准/拒绝”逻辑。

---

## 三、测试报告与验证指令

### 1. 契约测试（P0 提供，强制通过）

```bash
.venv/bin/python -m pytest tests/test_channel_contract.py -q
```

结果：**8 passed**（消息收发、DM 隔离、异步回调、历史升序、limit、广播、steer、协议实现）。

### 2. P2 模块测试

```bash
.venv/bin/python -m pytest tests/test_p2.py -q
```

结果：**5 passed**（持久化恢复、DM 顺序无关、慢订阅者超时、`ask` 取消、`answer` 解析）。

### 3. 全量测试

```bash
make test   # 等价于 .venv/bin/python -m pytest -q
```

结果：**146 passed, 1 failed**。

> 唯一的失败项 `tests/test_tool_contract.py::test_cancelling_a_tool_kills_its_child`
> 属于 **P1（工具层）** 的既有问题，与 P2 无关：
> `src/moumou/tools/builtin.py::run_process` 在取消/超时时只 `proc.kill()` 杀掉 shell
> 直连子进程，未杀整个进程组，导致 shell 派生的 `python3 -c ...` 孙进程存活，测试断言
> `ProcessLookupError` 失败。建议 P1 在 `create_subprocess_shell` 上加
> `start_new_session=True` 并用 `os.killpg` 清理整组进程。P2 不越界修改工具层代码。

### 4. 代码检查

```bash
python3 -m ruff check src/moumou/bus src/moumou/agents src/moumou/ui tests/test_p2.py
```

结果：**All checks passed**。

---

## 四、演示指南（README 补充）

### 启动 CLI 交互界面

```bash
# 在仓库根目录
.venv/bin/python -m moumou.ui.cli
```

无需 API Key，内置一段“群聊 → 私聊 → 通知 → 审批 → 提问”的脚本化演示：

- 看到审批卡片时输入 `y`/`n`（或 `yes`/`no`）决定同意/拒绝；
- 看到提问卡片时输入任意文本作为回答；
- 演示结束自动退出，终端干净关闭（无后台孤儿 Task）。

### 回放一次任务轨迹

先跑一次骨架演示生成 trace（无需 API Key）：

```bash
.venv/bin/python -m moumou.demo.skeleton   # 生成 runs/demo/trace.jsonl
```

再回放：

```bash
.venv/bin/python -m moumou.ui.replay runs/demo/trace.jsonl          # 时间轴
.venv/bin/python -m moumou.ui.replay runs/demo/trace.jsonl --task t1 --tree   # 单任务 span 树
```

时间轴按时间升序展示每条 span（谁派活、谁调工具、哪步等人、耗时与 token），底部附摘要。

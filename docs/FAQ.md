# 常见问题

## 环境

**`ModuleNotFoundError: No module named 'moumou'`**

用虚拟环境里的 Python：

```bash
.venv/bin/python -m pytest -q
```

或者直接 `make test`。不要用系统 python。

**改了代码测试还是旧的**

同上——直接敲 `pytest` 可能指向全局安装的版本。统一用 `make` 或 `.venv/bin/python`。

**`make install` 报找不到 uv**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

没有 uv 也行，手动建环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## API Key

**`make smoke` 报 `DEEPSEEK_API_KEY is not set`**

`.env` 不在仓库根目录，或者你是在别的目录跑的。所有命令都要求在仓库根目录执行。

**报 401**

key 错了，或者复制时带了空格。检查 `.env` 里 `DEEPSEEK_API_KEY=` 后面有没有多余字符或引号。

**模型名报错**

用 `deepseek-flash`。旧的 `deepseek-v4-flash` 也能用，但官方已标记为被替代。

**能不能多个人共用一个 key**

技术上可以，但不建议——额度会混在一起，出问题不好查。各自申请各自的。

## Git

**推之前要检查什么**

```bash
git status
```

确认里面没有 `.env`。这个文件不该出现在任何一次提交里。

**不小心把 key 提交了**

立刻去平台后台吊销这个 key（比改历史更重要），然后重新申请。

**分支怎么开**

一人一个长期分支，例如 `feat/p1-sandbox`、`feat/p2-cli`、`feat/p3-eval`。
合并前先 rebase 到 main，保证历史是线性的。

**commit 信息怎么写**

一句话说清做了什么，不要写 "update"。例如：

```
add path-jail sandbox with escape tests
```

## 测试

**我只想跑自己那块**

```bash
pytest tests/test_tool_contract.py -q      # P1
pytest tests/test_channel_contract.py -q   # P2
pytest tests/test_contracts.py -q          # 契约，谁都别改红
```

**契约测试红了，但不是我的问题**

先看是不是有人改了 `src/moumou/contracts/`。那个目录是冻结的，改字段会影响所有人。
确认是新需求的话，在群里说一声，由 P0 统一改并同步所有人。

## 运行

**`make demo` 会调真模型吗**

不会。它用的是写死的假模型，秒级完成，不消耗额度。可以放心反复跑。

**`runs/` 里的东西能删吗**

能。里面是运行产物（数据库、工作区、日志），全部在 gitignore 里。
删之前想留证据的话，先把 `runs/eval/report.md` 拷出来。

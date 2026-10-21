# 评测说明

## 怎么跑

```bash
# 需要 .env 里配好 key
make eval

# 只跑一道题，方便调试
.venv/bin/python -m moumou.eval.cli --only s1_hello

# 每道题跑 3 次（默认用 yaml 里的 repeats）
.venv/bin/python -m moumou.eval.cli --repeats 3
```

结果：

- 每道题一个独立目录 `runs/eval/<task_id>/r<N>/`，里面有自己的数据库、工作区和执行日志
- 汇总报告写到 `runs/eval/report.md`

## 题库格式

一个 YAML 一道题，放在 `eval/tasks/`。

```yaml
id: s1_hello                     # 唯一，重复会报错
title: 从零写一个可运行的脚本      # 给人看的
scenario: from_scratch           # 七种场景之一，见下表
goal: |                          # 给 Agent 看的需求原文，写清楚
  在当前项目里创建 hello.py...

acceptance:                      # 机器可判，不能写"代码质量良好"
  - kind: command
    target: python3 hello.py
    expect: "0"

setup:                           # 可选：开跑前往工作区塞初始文件
  calc.py: |
    def add(a, b):
        return a - b

human:                           # 可选：批量跑时脚本化人类的回答
  default: approve
  decisions:
    fs_delete: approve           # 按 action_class 指定
  answers:
    "文件名": "hello.py"          # 按提问关键词匹配

budget:                          # 可选：覆盖默认预算
  max_steps: 15
  max_wall_s: 300

repeats: 1
tags: [scenario-1, easy]
```

### 场景取值

| 值 | 含义 |
| --- | --- |
| `from_scratch` | 从零开发 |
| `debug` | 调 bug |
| `extend` | 在已有代码上加功能 |
| `research` | 联网调研 |
| `needs_approval` | 需要人工确认 |
| `use_memory` | 依赖历史经验 |
| `parallel` | 多 Agent 并行 |

### 验收项类型

| kind | target | expect |
| --- | --- | --- |
| `command` | 要执行的命令 | 期望退出码，默认 `"0"` |
| `pytest` | 测试路径，默认 `.` | 期望退出码，默认 `"0"` |
| `file_exists` | 文件路径 | 不需要 |
| `file_not_exists` | 文件路径 | 不需要 |
| `file_contains` | 文件路径 | 正则 |
| `json_key` | JSON 文件路径 | `a.b.c` 或 `a.b.c=值` |

## 指标口径

| 指标 | 定义 | 为什么这么定 |
| --- | --- | --- |
| **完成率** | 到达 `DONE` 的任务数 ÷ 创建的任务总数 | 任务包含子任务，所以拆得越多分母越大——这能反映"拆解是否有效" |
| **执行时间** | `submit` 到最后一个任务进入终态的墙钟时间 | 事件驱动调度，不含轮询噪声 |
| **token 成本** | 所有 span 的 `tokens_in + tokens_out` 之和 | 多 Agent 系统成本容易失控，这是真实问题 |

**每道题至少跑 3 次**，报告里给均值 ± 标准差。模型有随机性，跑一次就下结论站不住。

## 现有题目

| id | 场景 | 考什么 |
| --- | --- | --- |
| `s1_hello` | from_scratch | 最基础路径：写文件 → 跑命令 |
| `s2_bugfix` | debug | 先读后改，不能凭空重写整个模块 |
| `s5_approval` | needs_approval | 删除动作触发审批，批准后完成 |

还差 12 道，建议按七个场景各铺 2 道左右。

## 报告长什么样

`runs/eval/report.md`：

| task | scenario | runs | completion | std | wall (s) | tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| s1_hello | from_scratch | 3 | 1.00 | 0.00 | 42.1 | 18234 |
| ... | | | | | | |

底部给出总体完成率，并列出失败的任务。

## 待补

- 耗时分解（模型 / 工具 / 等人 三段）——需要在 span 上做聚合
- 消融实验（记忆关、并行关各跑一组）
- 图表（matplotlib 或直接 markdown 表）

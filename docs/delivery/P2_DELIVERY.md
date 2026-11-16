# P2 交互与通信层：兼容接入候选

[简体中文](P2_DELIVERY.md) · [English](P2_DELIVERY.en.md)

本候选保留原作者 `6f53165f720675311d1d21ab7a25e8e95f81dbc5` 提交，以当前主线
`0e030bd` 的 P1/P3 实现为兼容基准。当前只有工作区补丁，未暂存、提交或合并，新增回归尚未执行。
原 PR 旧工作目录中的通过/失败数字不能作为此候选的成绩。

## 接入边界

P2 提供消息持久化、人类交互队列、终端显示和只读回放。P1 继续负责工具、工作区和执行；
P3 继续负责老师题源、正式会话、审批/答复记录、搜索配额及质量指标。
本补丁不替换 Engine 默认总线，不改现有 P1/P3 源码，也不重新引入旧 YAML 题库。

| 模块 | 内容与限制 |
| --- | --- |
| `bus/sqlite.py` | 先保存后投递；严格的两成员 DM 历史；可绑定项目/会话和订阅范围 |
| `agents/human.py` | 原请求 ID 的 Future/队列交互、明确响应校验和取消；此类自身不是持久恢复后端 |
| `ui/cli.py` | `--run` 连接正式文件端口；`--demo` 单独保留脚本演示 |
| `ui/session.py` | 读取原请求与状态，安全暂存实际输入；不代替后端消费、授权或判分 |
| `ui/replay.py` | TraceEvent/JSONL、明确 task 的 TraceStore、正式运行目录和 P3 回放输入 |
| `tests/test_p2*.py` | P2 行为与边界回归源码，尚未执行；框架检查不等于老师题成绩 |

原 P2 测试从仓库根目录迁入 `tests/test_p2.py`。共享 Channel 检查保留原断言，
只将夹具接到有独立临时数据库、会关闭资源的 SqliteBus。

## 可选依赖

从已激活的 Python 环境安装界面扩展：

```powershell
python -m pip install -e ".[dev,p2]"
```

`rich` 只在 `p2` extra 中声明。原 P1/P3 默认依赖、开发依赖版本及已有锁文件保持不变。
兼容工作区没有复制主项目的真实 `.env`，本次也没有安装依赖或运行安装命令。
实际运行时按[环境说明](../SETUP.md)在对应目录配置课程服务。
UI 专属回归在缺少可选 Rich 时会跳过；跳过不算通过，完整验证应先安装对应 extra。

## 连接正式老师会话

两个终端必须指向同一运行目录。先通过原入口创建新会话：

```powershell
# 终端一；P3 仍执行原老师协议。
python -m moumou --human files --out runs/test/P2_SESSION

# 终端二；只连接已存在的会话，不自行启动模型。
python -m moumou.ui.cli --run runs/test/P2_SESSION
```

界面跟随 `active.json` 中的实际场景与阶段，读取 `human/requests`、`human/pending`，
展示原请求与可选答案。用户输入经校验后，以原 ID 发布到 `human/responses`。
不会覆盖其他控制器的响应，不为退出生成默认同意或伪造文本答复。

按界面展示的原 ID 输入命令，多个并行请求不会依赖“当前默认问题”：

```text
/pending
/answer REQUEST_ID 实际业务答复或原始选项
/approve REQUEST_ID 可选说明
/reject REQUEST_ID 拒绝说明
/history
/quit
```

`/approve`、`/reject` 仅用于审批或复核；业务问题必须使用 `/answer`。
空答、错误 ID 或不在原选项内的答复会被拒绝，保留待答请求供更正。

状态须分别理解：

- 待答文件只说明保存了请求，不证明后端进程仍活着。
- “已暂存”不等于 Agent 已消费；只有后端写入 consumed/事件后才算观察到消费。
- P3 继续执行原审批、脱敏、ledger 恢复、`/grant-search N` 回调及质量记录。
- 界面退出不默默终止独立运行的后端，也不自动回答留存请求。
- 文件端口没有 steer 命令协议，连接模式不伪造 `/steer` 队列或业务消息。

历史视图展示原记录及来源，模型/工具事件不冒充真实 Agent 群聊消息。

## 独立演示与回放

```powershell
python -m moumou.ui.cli --demo
python -m moumou.ui.replay runs/test/P2_SESSION --scenario scenario_1 --task t1 --tree
```

演示无需模型，只展示 P2 消息与输入机制；演示成功不是正式题质量接受。
回放保留场景+任务身份、原 span/父关系与原始/有效质量。缺失数据保持未知，
同 task ID 跨场景含混时须明确选择，不补造零耗时或父节点。

## 消息总线的范围与回调

正式消费者应绑定项目/会话或提供订阅范围。无绑定的两参数 subscribe 保留共享 Channel 兼容，
不是已认证的多租户权限层。`group` 且 members 为空的历史表示显式会话级操作员视图；
不能拿它当某个私聊参与者的权限过滤。DM 历史只包含指定两方直接来往，排除广播和第三方。

异步回调按契约等待并设上限。默认同步回调保持事件循环线程语义，必须快速返回；
可安全放入线程的阻塞回调可显式使用 offload 选项。超时移除订阅不代表强制结束 OS 线程，
也不构成任意回调的硬抢占保证。

## 待验证事项与历史保留

本候选没有执行 pytest、lint、编译、CLI 探针、模型或联网。后续验证应覆盖私聊/会话隔离、
原 ID 及选项校验、取消、原子答复发布、P3 文件队列联调、回放身份与真实 Windows 终端输入。
P1/P3 是否保持预期行为仍需实际回归确认；源码相同不是运行通过的替代。

原作者 commit 不 squash/rebase；兼容修复另作提交。最终实际 diff 和提交范围需用户确认后才操作，
当前主项目 main 工作区保持原样。共享协议与交付标准见[接口](../INTERFACES.md)、
[评测](../EVAL.md)和[交付要求](../DELIVERY.md)。

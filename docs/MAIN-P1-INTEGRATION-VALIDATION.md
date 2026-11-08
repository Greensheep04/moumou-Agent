# Main 与 P1 整合验证

日期：2026-11-08。负责人明确授权保留双方功能、解决冲突、完成测试并推送 main；此前文档中的“暂不合并 main”是历史轮次约束，本轮以当前授权为准。

## 基线与合并选择

- P1：`81aec709834faa0ce87de02f65c8c1469f9d30ff`，19 个提交尚未进入原 main。
- 远程 main：`7466c93938f3af3bc9a4b58908c93fa7a811da92`，相对本地旧 main 新增 7 个提交；整合前已重新 fetch。
- 使用保留双亲历史的 merge，不 squash、不强推。
- 唯一文本冲突为 `src/moumou/tools/builtin.py`；其余自动合并文件经过语义核对。

保留 P1 的工具注册、LocalJail 路径校验、独立 Git 工作区、审批、输出限制、Windows 挂起后加入 Job Object 的进程启动，以及可选的子任务机器验收。保留 main 的 SQLite TraceStore、JSONL 导入/恢复、Recorder 默认接入、pytest 依赖配置和文档更新。

`builtin.py` 保留 P1 拆分后的实现，同时保留 main 的 `python_command`。main 的临时 Windows `python3.cmd` 别名迁入 P1 统一执行器，支持复合命令，不替换源代码中的文字。main 的启动阶段取消与重复取消时等待清理的行为也迁入统一执行器，取消的 Windows 挂起进程不会恢复执行。

## 验证证据

- 合并前 P1 全量：293 passed。
- 初始联合回归：5 failed、1 passed，复现复合 python3 命令、带空格 pytest 路径及尚未接入 SQLite 的差异。
- 清理增强前确定性回归：2 failed，分别复现启动交接期间取消和清理期间重复取消提前返回。
- 修复后执行/验收联合专项：23 passed；新增联合回归单独复核：8 passed。
- 远程历史 `275b7e7` 中的 TraceStore 测试仅提取至被忽略的 `runs/main-p1-validation/` 额外验证：25 passed，覆盖排序、幂等、冲突回滚和持久化。
- 最终全量：301 passed in 145.83s。
- Ruff `check --no-cache src tests` 与 Git 差异空白检查通过。
- 离线 P1 demo：3 done、0 dead，脚本输出 `hello, moumou`；交付冲突被识别且原文件回滚保留。
- 独立只读审查发现的取消处理缺口已补齐；复审未发现新的阻塞项。

新增 `tests/test_main_p1_integration.py` 作为受版本控制的联合回归；保留 main 忽略其他本地新测试的规则，不恢复其已删除的本地测试文件。

## 验证边界与协作流程

本轮为离线功能整合，没有新增真实模型或搜索 API 调用。Windows 本地测试通过后推送；新 SHA 的 Windows/Ubuntu Actions 必须单独核对，历史绿色运行不能替代。本报告记录提交前验证，推送后远程 SHA 与 CI 结果在本次交付消息中报告。

后续修改前先检查工作区和远程差异、获取并整合最新 main；验证通过后提交，推送前再次 fetch。若 main 并发更新，先整合并复验，再普通推送，不强制覆盖队友提交。密钥、`.env`、数据库和 `runs/` 不进入提交。

# 评测指标契约

[简体中文](metrics.zh-CN.md) · [English](metrics.md)

本契约供编排、人工交互、报告与回放模块共同使用。正式评测通过
`python -m moumou` 或 `python -m moumou.eval.cli` 加载 `eval/tasks/`
中的老师原始 Markdown。权威计划为 `RUN/test.json`，协议标记为
`kind: instructor_test`；交付观察来自 `task_1-result.json` 和实际产生的
`task_2-result.json`。不能从控制台的一句成功提示推算成绩。

`tests/` 是代码功能、接口和回归测试；即使其中检查了题库加载器，也不等于执行了老师题目。
`dataset_kind` 保留 `instructor` 及历史/外部题源的 `supplementary` 标记，场景的 `source_kind`
与源文件哈希保留作者和来源；这些兼容字段不表示仍随附另一题库。同一执行协议不会把外部题变成老师题。

## 根目标、完成率和质量

`planned_root_goals` 来自实际选择的源任务文本：唯一随附的完整老师 mid-test 为六个目标，单个老师场景为两个；
历史或外部 Markdown 题源按各自实际任务计划计数，不预设固定题数。不同来源分别计算，未发布、被中断、质量未审阅的目标
仍保留在原计划分母中；这些状态本身不能证明 Agent 已执行或人类已拒绝。

| 字段或观察 | 含义 |
| --- | --- |
| `agent_declared_complete` / `root_status == DONE` | 框架已到达该任务的完成状态 |
| `root_machine_criteria_present` | 根任务声明了可执行的机器验收条件 |
| `root_machine_accepted` | 保存的机器验收结论；缺少足够机器证据时为 null |
| `quality_status` | 真实接受、拒绝、等待审阅或尚未审阅的质量状态 |
| `verified_goal_success` | 真实质量接受、根状态为 DONE 且没有相反机器失败时才可为 true；质量未知时保留 null |

空验收条件属于自报完成。Agent DONE、子任务测试通过、工具执行授权，都不能单独替代质量审阅。
实际人类拒绝质量后保留 Agent 原状态和产物，跳过该组后续依赖任务；已经配置好的其他独立组
可以继续。初始化、配置、清理失败及全局取消会停止整场；未尝试的目标仍为未知。
`--quality-review deferred` 明确保留未知质量。

离线复核是追加记录，保留 `raw_quality`，另提供有效质量、`quality_source` 和审阅历史。
每次决定绑定原任务结果、执行版本、输入和产物哈希；重新审阅必须通过 `--supersedes`
指向当前审阅链末端。记录一致不等于密码学身份认证，也不能证明每次 HumanIO 响应都由人重新输入。

`goal_metrics` 报告完整计划分母、已保存交付数、Agent 完成数、已审阅数、已核验成功数与覆盖率。
只有 `verified_goal_success == true` 进入成功分子。未审阅和未执行应单列，不能虚构为模型失败或接受。

## 上下文、子任务与持续团队

正式适配器优先保留完整原角色、当前目标、全部验收和当前任务已答信息，可选历史、依赖、结论板和
经验内容先缩减。辅助原文保存到私有 `.evaluation/context/<digest-prefix>.json`，
新文件名使用 SHA-256 的前 32 位，引用仍保留完整哈希；不同内容碰撞明确拒绝，旧档案不重命名。
必需内容放不下时抛出 `ContextAdmissionError`，不将被截断要求交给模型。

归因应检查实际发送的模型输入。框架漏发要求或已答信息属于系统/上下文问题，不能据此声称模型
无视了它看见的回答；失败保留原分母，修复后的版本与运行另行记录。

每条任务结果只包含该根目标的子树。`observed_subtasks` 排除根节点，
`observed_subtasks_done` 统计观察到的 DONE 子节点。声明了多少验收条件不等于条件已通过；
具体是否执行和通过，需看保存的 acceptance 事件。

同一场景的 Task 1、Task 2 共享四个身份、各自私有目录、记忆和真实交付历史，属于连续依赖请求。
不同场景使用分离的项目状态。第二次 `Engine.run()` 的整体统计可包含前一次任务，
任务专属指标应使用各自子树与 phase，不能将整组累计数说成 Task 2 专属数。

已答问题按 project/conversation/task/role 保存，保留原请求 ID、正文和原 Agent 来源，
恢复后固定进入 opening；待答问题沿用原身份。工具审批仍逐次进行。
`parent_span` 是执行事件层级，`parent_id` 是任务树关系；新自动采集不会补造旧历史中缺失的关系。

## 保存的阶段证据

| 字段 | 证据边界 |
| --- | --- |
| `artifact_snapshot` | 不可覆盖的交付清单和文件副本，保留原相对路径、时间、完整哈希及缺失/排除/部分状态 |
| `memory_snapshot` | engine 运行后的 project:role 活跃记忆元数据与原始观察，不代表仅本题写入或删除总数 |
| `failure_snapshot` | 实际保存的工具失败/错误事件，计数可能为 null，不把缺失记为零 |
| 场景 `state_snapshot` | 收尾时包含未完成节点的诊断；不增加已交付目标或计划分母 |

`memory_retrieved`、`memory_context_selected`、`memory_prompt_observed`
分别描述返回的候选、上下文选取和保守的完整提示词匹配。截断、多行或身份含混的经验仍未知。
出现于输入不等于模型实际使用，更不证明因果收益。

离线报告与回放优先使用这些阶段记录，不用当前可变工作区或实时 `memory.db` 倒推过去。
旧运行没有保存的快照或观测仍为未知。[性能报告](PERFORMANCE.md)记录当前版本实际验证状态；
接口说明、用例源码和旧版本测试成绩均不能替代新版本验收。

## 时间边界

| 字段 | 范围 |
| --- | --- |
| `elapsed_s` / `delivery_elapsed_s` | 从发布任务到 Agent 交付及框架维护返回，位于质量审阅之前 |
| `quality_review_wait_s` | 交付后等待实际人类质量决定的时间 |
| `total_to_review_s` | 发布任务到完成所配置审阅阶段 |
| `suite_wall_time_s` | 保存的带时区 `started_at` 与 `finished_at` 之差，缺任一端点则未知 |
| `delivery_time_s` | 仅保存交付样本的汇总，不是整场总耗时 |

交付时间包含执行期间的工具调用、澄清与工具审批等待；质量审阅额外记录。未完成或强制中断的审阅
不能补一个零耗时。任务用单调时钟计时；离线整场时长仅使用原保存时间，拒绝无时区、无效或逆序值。
没有端点时不能用当前时间或各交付时长之和补算。`n=0, sum=0` 只表示没有交付样本。

人类、工具和 LLM 的累计 span 可因并行而重叠，不能盲目相加或相减为端到端墙钟时间。
循环里的每节点时间预算属于协作式检查，不是等待真人会话的整进程硬截止时间。

## 模型用量和搜索

`RecordingLLM` 在场景 `dialogue.jsonl` 保存脱敏后的实际请求、响应、phase、调用身份和时间。
人类事件保留原请求、响应、请求 ID 与等待时间。这些本地记录可能包含任务数据，不随源码提交。

任务的 `llm` / `human` 汇总属于当前 phase；场景汇总跨两题累计。某次调用缺少 provider usage 时，
保留已知小计，完整 token 总计为 null。脚本测试计数不等于服务计费；内部 HTTP 重试、失败调用计费、
缺报缓存和价格均不能从默认零值推断。学校模型使用额度与 Tavily 搜索额度独立。

`search_delta` 描述任务前后工具调用、实际 provider 尝试、缓存、并发去重和拒绝的变化；
`search_scenario_cumulative` 是共享场景累计快照。初始六次新 Tavily HTTP 尝试由全组四人、两题共享，
缓存保留原检索时间，并发相同请求不必增加 provider 尝试。工具返回额度不足不证明已询问或取得批准。
追加额度需要真实、明确且保留来源的批准，同一批准 ID 重复恢复不能重复增加额度。

## 离线重建与历史

`python -m moumou.eval.report --run DIRECTORY` 从已保存结果生成 Markdown，不调用模型、
执行验收或恢复任务。原计划和每任务文件为依据，不把未回答的质量请求变成成功。
manifest、任务结果和日志不是跨文件事务；强制停止可能留下 raw `running`，应保留并附实际停止证据，
不能伪造正常完成、质量拒绝或已接受。

`report_builder.source_sha256` 标识生成报告的代码，原 manifest 的 `code` 标识实际执行的版本。
重新生成报告不会改写运行来源。报告和回放之间重读任务内容还需匹配原验证对象的规范 JSON 哈希。

旧独立 batch/ablation/result/telemetry API 用于历史档案和内部检查，不构成另一套正式入口。
不能将它们的独立尝试、记忆比较和统计结果混入老师连续会话。重复实验需要独立会话标识、兼容设置和
真实样本数；一次观察没有样本标准差，连续两题的耗时差也不证明记忆收益。

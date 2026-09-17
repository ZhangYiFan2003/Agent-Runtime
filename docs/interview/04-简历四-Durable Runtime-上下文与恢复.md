# 简历四：Durable Runtime、上下文与恢复

> 本册回答“长任务怎样保存、恢复、控成本、避免死循环，以及上下文如何不失控”。

## 0. 简历原句

> 基于 SQLite 持久化 Run / Checkpoint 与工具执行记录，在模型调用与工具执行边界保存状态；通过 CAS 避免并发冲突，并记录工具调用标识防止重复执行，结合执行次数与超时限制支持断点恢复与幂等重试；通过 40 个故障注入场景验证中断后的状态恢复能力。

> **源码审计后的安全口径：** “记录工具调用标识防止重复执行”只对已经持久化为 `SUCCEEDED` 的同一逻辑调用成立；“幂等重试”依赖只读语义或下游幂等契约。SQLite 现在持久化显式 retry state、attempt、失败分类、suppression reason、UTC `next_retry_at` 和累计 backoff，因此 unsafe `UNKNOWN` 的保守停止可跨重启保持；它仍不等于外部副作用 exactly-once。故障注入数字必须按 runner 的命名场景数、重复执行数、recovered、expected-safe-stop 和 failed 分开报告，不能把执行次数写成独立场景数。

## 1. 这条经历解决了什么问题

Agent 任务比普通 HTTP 请求更长，也更有状态。一次任务可能经历多轮模型调用、Tool 副作用、人工审批和 Child Run；如果所有状态只在协程栈和消息列表里，进程退出后只能从头执行，既浪费 Token，也可能重复外部写操作。

另一个问题是上下文会不断增长。直接保留全部消息会提高成本和模型首字延迟，甚至超过窗口；粗暴截断又可能丢掉目标、约束、尚未完成的工作和 Tool 协议配对。Axiom 把执行持久化、Tool 去重、上下文压缩、预算和无进展检测作为一组长期运行治理能力。

## 2. 30～45 秒主回答

我把 durable Run 看成一个持久化状态机。Runtime 在 LLM、Tool、interrupt 等关键边界保存带版本号的 Checkpoint，并用 CAS 拒绝旧版本覆盖新状态；Tool 调用使用稳定 invocation ID，成功结果写入 ToolExecution，恢复时相同调用可直接复用。

长上下文由 ContextManager 在每次模型调用前重新估算：保留系统协议、当前目标、近期原始消息和结构化任务状态，对较早内容做摘要，对过大的历史 Tool 输出做投影，并在硬上限前 fail closed。RunBudget 同时限制步骤、模型/工具调用、Token、时间和成本；ProgressDetector 检查重复动作、重复错误、短循环和状态停滞。当前 durable truth 可选本地 SQLite 或共享 PostgreSQL，但共享存储不等于分布式 Worker 接管，也不提供外部写 exactly-once。

## 3. 2～3 分钟完整故事

我先把会话和执行状态分开。Thread 管会话范围；Run 是唯一 durable 执行、控制、ownership 单元；Checkpoint 只是 Run 的版本化 durable state 机制；Step 是 Run 内临时迭代；Event 保存历史/SSE 证据；ToolExecution 保存外部调用事实。`turn_id` 只保留交互关联兼容性，不是恢复实体。

Step Contract 只统一一次迭代的内存输入输出：`StepContext` 引用当前 RunState、控制状态与 Run-level ownership，`StepResult` 返回 `CONTINUE/COMPLETE/WAIT/FAIL` 和轻量 Tool invocation ID。它没有表、Repository、lease 或 fencing；崩溃后丢失 StepResult 是安全的，因为恢复事实仍在 RunState 与 ToolExecution。

每个 Checkpoint 有版本号。保存时使用 compare-and-swap（CAS，比较并交换）：只有数据库中的版本等于调用方预期版本，才允许写入新版本。SQLite 用 `BEGIN IMMEDIATE` 比较最新 sequence 后追加版本；PostgreSQL 用单条 `UPDATE runs ... WHERE current_sequence = expected RETURNING` 原子推进 head，并在同一短事务追加 Checkpoint 历史。两种实现都能拒绝 stale write，但 CAS 本身不等于持续执行所有权；多 Worker 仍需 lease 和 fencing token。

Tool 恢复是最需要诚实的地方。稳定 invocation ID 由 Run 与 Tool call 身份派生，并保存参数哈希。若记录已是 `SUCCEEDED`，恢复时可以复用结果；若进程在 Tool 外部副作用成功后、写成功记录前崩溃，数据库里可能仍是 `RUNNING`，此时结果未知。已分类的 unsafe timeout 会持久化为 `UNKNOWN + RETRY_SUPPRESSED`，重启不会重新执行；读操作或带下游幂等契约的 Tool 才适合按剩余 attempt 和 UTC backoff deadline 自动重试。这里说“Tool 结果成功持久化”，不要把它叫成 Kafka/Redis Streams 的消息 ACK。

上下文管理不是简单“超过长度就 summary”。ContextManager 先组成消息单元，保护 tool_call/tool_result 配对与最新用户目标，复用已有结构化摘要，将历史超大 Tool 输出投影为名称、状态和裁剪内容。达到高水位才压缩到目标区间；若固定内容本身超过硬上限，在调用 LLM 之前就失败。Map-Reduce 摘要也用于长会话记忆，但执行态摘要和长期事实不能混为一谈。

最后用预算、deadline 和无进展检测封顶。预算在调用前预留、调用后按实际用量核销，Child Run 消耗归集到 root ledger；未知模型价格标成 unknown，而不是按 0 元。LLM/Tool attempt 的 timeout 会取配置值和 root Run 剩余 lifetime 的较小值；暂态失败的每次 retry 仍占普通调用预算。ProgressDetector 使用稳定指纹识别同动作、同错误、2～4 步短循环和状态无变化，先给恢复提示，仍不收敛则以 `NO_PROGRESS` 终止。

## 4. 架构与调用链

```text
                    Durable truth
                         │
        ┌────────────────┼────────────────┐
        │                │                │
       Run          Checkpoint      ToolExecution
        │                │                │
        └──── Durable Store contracts ────┘
                    │            │
             SQLite(default)  PostgreSQL(shared)
                         │ restart
                         ▼
                reconstruct execution
                         │
                         ▼
               process-local execution
                         │
                 asyncio Task / loop
                         │
                         ▼
               ActiveRunSupervisor
```

必须反复强调：**shared durable state != live process ownership**。SQLite 或 PostgreSQL 里的状态能让进程重建执行；`asyncio.Task` 和 `ActiveRunSupervisor` 只代表当前进程里谁正在跑。现在 PostgreSQL Worker 另有 durable runnable metadata、原子 claim、数据库时间 lease、heartbeat 和单调 fencing token：它们约束谁能继续提交 Runtime 状态，并在 lease 过期后由 claim loop 自动接管；SQLite 仍只支持单节点。

这几层不要混淆：`FOR UPDATE SKIP LOCKED` 只在短事务内避开并发候选，事务提交后锁就消失；长任务所有权来自 lease + fencing，而不是把事务保持到 LLM/Tool 结束。Checkpoint CAS 防同一 sequence 的 stale state，fencing 防旧 ownership generation；heartbeat 无法证明权限时 fail closed。fencing 只能拒绝旧 Worker 写 Runtime truth，不能撤回已经发往外部系统的副作用。

## 5. 最新源码实现

- `src/axiom/runtime/models.py`：`RunStatus`、`Checkpoint`、`ToolExecutionRecord`、`BudgetLedgerRecord`。
- `src/axiom/runtime/checkpoints.py`：Memory/SQLite store、Checkpoint CAS、ToolExecution 和版本化 budget ledger；SQLite 开 WAL、`busy_timeout=30s`。
- `src/axiom/runtime/storage.py`：把 RuntimeStore、EventRepository 和 ControlOperationStore 组合成同一 backend，默认 SQLite，配置 PostgreSQL 时不静默降级。
- `src/axiom/runtime/postgres.py`：psycopg 3 bounded pool、PostgreSQL schema/version、Run head CAS、Checkpoint history、ToolExecution、budget ledger、Event 与 control idempotency。
- `src/axiom/runtime/events.py`：EventRepository contract 与 SQLite event backend；PostgreSQL Event ID 使用 identity/BIGINT，保证 replay 顺序但不追求无空洞。
- `src/axiom/runtime/durable.py`：`start/resume/interrupt/cancel`、LLM/Tool 边界、稳定 invocation、恢复和错误状态。
- `src/axiom/context.py`：`ContextBudgetPolicy`、`RuntimeContextSummary`、`ContextManager`、Tool 投影和硬上限。
- `src/axiom/runtime/budget.py`：策略、Decimal 定价、预留/核销、root Run 聚合。
- `src/axiom/runtime/progress.py`：动作/错误/状态/证据指纹、短循环和恢复提示。
- `src/axiom/memory/summarizer.py`、`memory/context.py`：Map-Reduce 会话摘要和记忆上下文；它们与 Runtime Context projection 是相关但不同的层。
- `tests/test_durable_runtime.py`、`test_context_management.py`、`test_run_budget.py`、`test_progress_detection.py`：关键证据。

## 6. 必须讲清的技术点

**Checkpoint** 不是数据库快照，而是某个 Run 可恢复的最小执行状态：消息、状态、待执行调用、策略状态、版本和错误等。保存边界要足够细以减少重做，也不能每个 token 都写数据库。

**CAS** 解决旧写覆盖新写：调用方带预期版本，数据库原子检查后更新。它解决状态竞争，不自动解决“哪台 Worker 有权执行”。

**Context** 是本次 LLM 真正看到的投影；**Memory** 是可跨 Turn/Thread 检索的事实或摘要；**Checkpoint** 是恢复执行所需状态。三者目标不同。

**预算** 是运行前后的硬治理，不只是最终统计。预检避免明知超额还发请求，核销处理估算与真实 usage 的差异。

**Timeout 与 deadline** 不是同一个边界：timeout 限制一次 LLM/Tool attempt，deadline
限制整棵 Run 的剩余 lifetime。Tool 配置 10 秒而 root Run 只剩 2 秒时，有效 timeout
最多约 2 秒；下次 backoff 已经放不进剩余时间时不再 retry。每次真实 model request
attempt 和 Tool attempt 都照常消耗调用、wall time，以及 Provider 实际报告的 Token/成本，
retry 不能绕开 Budget；恢复时复用已成功 ToolExecution 则不重复收费。

**无进展** 不等于失败一次。它是连续动作/错误/状态没有新证据的模式，需要稳定指纹和阈值降低误杀。

## 7. 设计理由、替代方案与取舍

为什么在 LLM/Tool/interrupt 边界保存？LLM 和 Tool 都昂贵或有副作用，恢复时最值得避免重做；每个 token 保存则写放大严重。为什么 SQLite？本地零运维、事务和 WAL 足够支撑当前单 Runtime；共享服务才需要 PostgreSQL。

上下文可选滑动窗口、纯摘要或检索式记忆。滑动窗口简单但可能切掉关键约束；纯摘要有语义损失；Axiom 采用 pinned/recent/summary/tool projection 组合，并在超限时拒绝调用。目标压缩比只是调度目标，不应声称每次严格收敛。

外部 Tool 无法仅靠本地数据库实现 exactly-once。更严格方案需要对方接受幂等键、提供 operation status 或支持事务/补偿。文档必须区分“本地状态不重复提交”和“外部世界绝不重复产生副作用”。

## 8. 故障与边界场景

- 两个 resume 同时写同一 Run：CAS 让一个成功，一个收到冲突并重新加载。
- LLM 返回一半时进程退出：从上一个已保存边界恢复，可能重做未完成模型调用。
- Tool 成功但记录未落库：外部结果未知；只对安全操作重试。
- Checkpoint 太频繁：SQLite 写竞争与磁盘 I/O 上升；太稀疏则恢复重做增加。
- 摘要模型失败：不应破坏原 Checkpoint；使用未压缩上下文或给出明确预算错误。
- pinned 内容超过窗口：调用 provider 前失败，不能静默删目标或协议。
- Child Run 共同消耗预算：根 Run 账本原子聚合，避免每个子任务都以为自己还有全额预算。
- Tool timeout 配 10 秒但 root Run 只剩 2 秒：有效 attempt timeout 被截短到剩余 lifetime，
  外层 wall deadline 优先。
- 价格表未知：成本可用性标记为 unknown，不能把未知成本当 0。

### 8.1 Crash-window matrix

| 崩溃点 | 当前 durable evidence | 当前恢复行为 | 风险/边界 |
| --- | --- | --- | --- |
| Tool 开始前 | 没有成功记录；可能只有 pending Checkpoint | 正常执行该逻辑调用 | 低，但仍受权限/预算约束 |
| `ToolExecution=RUNNING` 已保存、Tool 尚未真正执行 | `RUNNING` | 只读/显式幂等调用可重试；普通写进入歧义处理 | 本地无法证明远端没执行 |
| 远端 Tool 成功、本地 `SUCCEEDED` 未保存 | 若进程直接崩溃，通常仍是 `RUNNING` | 不安全写等待显式 recovery decision | 重复副作用风险最高 |
| timeout 被分类为 `UNKNOWN` 并抑制 unsafe retry，随后在父 Checkpoint 前崩溃 | SQLite 有 `UNKNOWN/attempt/category/retry_state/suppression` | 重载后复用错误证据，不自动执行 Tool | 外部结果仍未知；需要 status query、幂等证据、人工或补偿 |
| `ToolExecution=SUCCEEDED` 已保存、父 Checkpoint 未推进 | 成功结果和参数哈希存在 | 同一 invocation 复用结果，再推进 Checkpoint | 避免再次收费/再次调用 |
| 父 Checkpoint 已保存 | 完整可恢复状态 | 从最新 sequence 继续 | 正常恢复路径 |
| LLM 请求已发出、响应未写 Checkpoint | 只有上一个语义边界 | 可能重放模型请求 | 重复成本；不能凭空还原响应 |
| Parent `CANCELLED` 已保存、Child cancel 尚未完成 | Parent 已终止，Child 可能仍非终态 | 显式 Child resume/执行 preflight 检查全部 durable ancestors，并把非终态 Child 收敛为 `CANCELLED` | 没有自动后台 Recovery Scanner；无人 resume 时不会主动扫描 |

边界 checkpointing 只是把窗口变清楚、变小，不会消灭所有窗口。尤其是 SQLite 与外部系统之间没有共同事务，不能用“Checkpoint 可以恢复”概括所有情况。

### 8.2 故障注入证据到底是多少

不要背固定数字。运行 `benchmarks/recovery/scenarios.json` 对应 runner 后，分别报告命名场景数、重复次数形成的 execution count、`recovered`、`expected-safe-stop` 和 `failed`。本轮 Durable Semantics Closure 的 pytest 矩阵单独覆盖 retry pending/attempt/backoff/exhaustion/UNKNOWN、成功复用、取消/deadline，以及 Parent/Child crash window、显式 resume、CAS race、完成 Child 和 live cancellation；它不应被包装成旧 runner 的新 baseline。

## 9. 分级面试题库（28 题）

### P0 — 简历直击题（16 题）

#### P0-1｜“为什么分 Thread、Turn、Event、Run、Checkpoint 这么多对象？”

**面试官为什么问：** 简历一次列出五类状态，面试官会检查它们是否只是重复建模。

**先给结论：** Thread 是会话容器，Run 是唯一 durable 执行实体；一个交互可触发 Root Run，复杂执行再产生 Child Run。Event 用于历史重放，Checkpoint 是版本化 Run state 的保存机制，Step 只存在于内存执行循环。

**60～120 秒完整口语答案：** Thread 是会话容器，Event 是可追加、可按 ID 重放的历史事实。Run 是一次可控制、可恢复、可调度的 Agent 执行，复杂模式下还有 Child Run；`RunState` 在安全边界保存最新可恢复状态，Checkpoint 是它的兼容/历史名称。Event 展示“发生过什么”，但恢复不依赖全量 Event replay。Turn 没有表、Repository 或状态机，只剩 `/turns` API 与 `turn_id` 关联兼容；Step 没有持久化。

**第一轮追问：** “Turn 为什么不再是核心对象？”——当前 correctness 只需要用户事件触发 Root Run；控制、预算、ownership、恢复和 Child lineage 都属于 Run。`turn_id` 可继续关联 UI/Event，但不参与 CAS、fencing 或幂等。

**第二轮追问：** “Event 和 Checkpoint 会不会不一致？”——它们是不同目的的数据；关键状态以 Checkpoint 为恢复事实，事件用于可见历史。生产化可用同一事务/outbox 缩小跨表不一致窗口。

**常见坑：** 把五者都解释成聊天消息；说 Event 日志可以直接替代业务状态。

**项目证据：** `src/axiom/runtime/api.py`、`src/axiom/runtime/models.py`。

#### P0-2｜“Checkpoint 具体存什么？为什么不能只存消息历史？”

**面试官为什么问：** 检查你是否理解恢复的不变量，而不是“把 messages 序列化”。

**先给结论：** Checkpoint 除消息外还要保存 Run 状态、版本、父子关系、pending Tool、策略状态、interrupt/error 等恢复事实。消息无法表达一个 Tool 是否已经提交、计划哪些节点已完成或当前是否等待审批。

**60～120 秒完整口语答案：** 如果只存消息，崩溃后看见最后一条 assistant Tool Call，却不知道调用是否开始、是否成功或是否可安全重试；Plan/Multi-Agent 还需要 DAG、assignment 和 Child 状态。Checkpoint 因此保存推进状态机所需的最小结构化数据，而不是 dump 整个 Python 进程。它要可版本化、可序列化、可迁移，避免把事件循环 Task、连接对象等瞬态资源放进去。

**第一轮追问：** “为什么不每一步 dump 全部内存？”——不可移植对象多、体积大、版本脆弱，还会把连接和锁的偶然状态当业务事实；显式状态模型更可测试。

**常见坑：** 说 Checkpoint 就是聊天记录备份；把进程快照与业务状态持久化混淆。

**项目证据：** `Checkpoint` 模型、`src/axiom/runtime/checkpoints.py`、durable tests。

#### P0-3｜“为什么选 LLM、Tool、interrupt 这些边界保存？频率怎么权衡？”

**面试官为什么问：** 简历明确写了保存边界，面试官会追写放大与恢复重做。

**先给结论：** 这些边界要么昂贵、要么有外部副作用、要么改变控制状态，保存后能最大幅度减少重做和歧义。太频繁会增加序列化和 SQLite 写竞争，太稀疏会在崩溃后重复更多工作。

**60～120 秒完整口语答案：** LLM 调用前后保存，可以知道昂贵请求是否完成；Tool 前后保存 pending 与结果，可以识别副作用窗口；interrupt/审批改变可恢复控制状态，也必须落库。没有必要为每个流式 token 写一次，因为它会把数据库变成主瓶颈，而且半条模型输出通常也难以安全续接。策略是选择语义稳定的提交点，再通过 Trace 观察 checkpoint latency 和恢复重做成本。

**第一轮追问：** “LLM 已调用完但保存失败怎么办？”——从上一个 Checkpoint 恢复时可能重做模型调用；模型输出没有外部写副作用，但会增加成本，必要时可用 provider request ID/响应缓存缩小。

**常见坑：** “保存越频繁越 durable”；忽略数据库关键路径和半完成结果语义。

**项目证据：** `DurableAgentRuntime._execute_llm_step()`、`_execute_pending_tool()`、checkpoint spans。

#### P0-4｜“Checkpoint 为什么要版本号？CAS、事务和锁有什么区别？”

**面试官为什么问：** 这是持久化 claim 最自然的并发基础题。

**先给结论：** 比较并交换（CAS）用预期版本拒绝旧写，属于乐观并发控制；数据库事务保证一组读写原子性；锁通过互斥阻止同时进入。三者可以组合，但解决的问题不同。

**60～120 秒完整口语答案：** 例如两个请求都读到 v5，一个先写成 v6；另一个若无版本条件，可能用旧状态覆盖 v6。CAS 在事务中检查数据库仍是 v5，才写 v6，否则抛冲突让调用方重载。进程内 Run lock 可以减少同进程竞争，但重启后不存在；数据库事务保证检查与更新不可被插入。CAS 不等于幂等：它防 stale write，重复相同业务操作是否产生副作用仍需操作身份和状态记录。

**第一轮追问：**

- **追问：** “既然有事务，为什么还要版本号？”
  - **回答思路：** 事务只保证本次原子，不能知道调用者依据的数据是否陈旧。
  - **30～60 秒可直接说出的答案：** “事务能保证检查和写入一起提交，但如果不带预期版本，数据库仍会合法接受一个基于旧快照算出的状态。版本号把调用方的前置条件编码进更新，因此能检测 lost update。”

**第二轮追问：** “CAS 能解决两个 Worker 同时执行吗？”——只能阻止最终旧状态覆盖，不能阻止两个 Worker 同时调用外部 Tool；多机还需要 lease/claim/fencing。

**常见坑：** 把 CAS、锁、事务、幂等当同义词；说 CAS 能保证分布式单执行者。

**项目证据：** `SQLiteCheckpointStore`、`CheckpointConflictError`、CAS tests。

#### P0-5｜“进程挂掉以后，具体怎样恢复一个 Run？”

**面试官为什么问：** “支持进程重启恢复”是可验证的强 claim，必须能讲具体流程和不能恢复的瞬态对象。

**先给结论：** 重新加载最新 Checkpoint，校验当前状态和版本，重建 LLM/Tool/策略依赖，再由 `resume` 从 pending 边界推进；父 Run 还要重新对账 Child 状态。

**60～120 秒完整口语答案：** Runtime 不恢复原协程栈，而是重新构造 DurableAgentRuntime，读取 Checkpoint 中的消息、策略状态、pending Tool、interrupt 和版本。如果处于可恢复状态，`resume` 根据策略继续；如果有已完成 ToolExecution，按稳定 invocation 复用结果；Plan/Multi-Agent 父 Run读取 Child Checkpoint 重新计算就绪或汇合。SQLite 恢复仍由本地 API/调用链触发；PostgreSQL Worker 的 claim loop 会在 lease 过期后自动取得更高 fence 并 resume。ActiveRunSupervisor 的内存 Task 始终不会跨进程保存。

**第一轮追问：** “恢复后两个请求同时 resume？”——同进程锁减少竞争，Checkpoint CAS 决定谁成功推进；失败方重新加载或返回冲突。

**第二轮追问：** “连接、Task 怎么恢复？”——不恢复；它们是瞬态依赖，按持久状态重新创建。

**常见坑：** “程序从崩溃那一行继续”；“SQLite 有记录就会自动启动”。

**项目证据：** `DurableAgentRuntime.resume()`、`_advance()`、`_reconcile_children()` 相关测试。

#### P0-6｜“Tool 已经执行成功，但结果没写进 Checkpoint，你怎么知道？”

**面试官为什么问：** 这是 durable Tool 最关键的 crash window，直接测试你有没有夸大 exactly-once。

**先给结论：** 本地无法仅凭 Checkpoint 确定外部副作用是否发生，这时结果是 unknown。稳定 invocation ID 和 ToolExecution 能识别逻辑调用、复用已确认成功结果，但未知写操作仍需外部幂等键、状态查询或补偿。

**60～120 秒完整口语答案：** 执行前先写 ToolExecution RUNNING，成功后再写 SUCCEEDED 与结果。如果进程在外部服务成功后、本地更新前崩溃，恢复看到的是 RUNNING，不能假设没执行。读 Tool 通常可重试；创建工单、发消息、支付等写 Tool 应把稳定 operation ID 传给服务端并由唯一约束去重，或查询该 operation 的状态。对方不支持时，只能人工确认或补偿，Axiom 不宣称跨 SQLite 和外部系统的原子 exactly-once。

**第一轮追问：** “stable invocation ID 怎么保证稳定？”——由稳定 Run/Tool call 身份派生，而不是每次进程启动随机生成；同时校验参数哈希，防止同 ID 换参数。

**第二轮追问：** “结果复用会不会复用错？”——只有逻辑身份和参数哈希一致、状态明确 SUCCEEDED 才复用；Tool 版本/外部时效仍是更高层契约。

**常见坑：** “RUNNING 就直接重试”；“本地数据库能保证外部 exactly-once”。

**项目证据：** `ToolExecutionRecord`、`src/axiom/runtime/checkpoints.py`、durable Tool tests。

#### P0-7｜“进程在 retry 第二次后挂了，恢复会不会从零开始？”

**先给结论：** SQLite 路径不会把 attempt 重置为 0：同一逻辑调用沿用稳定 `invocation_id`，表中持久化 attempt、失败分类、显式 retry state、suppression reason、UTC `next_retry_at` 和累计 backoff。第二次后 crash，恢复最多只剩第三次机会。

**60～120 秒完整口语答案：** 每次真实 Tool attempt 之前先在预算账本消费 Tool call，再把递增后的 attempt 与 RUNNING 状态落库。暂态失败会把 `RETRY_PENDING` 和 UTC deadline 一起保存；重启前 deadline 未到就继续等待，到期才消费下一次 attempt。`RETRY_EXHAUSTED` 和 `RETRY_SUPPRESSED` 都是终止型 durable decision；unsafe timeout 的 `UNKNOWN + RETRY_SUPPRESSED` 不会在 restart 后重新执行。`SUCCEEDED` 结果复用仍然不收费、不重跑。

**第一轮追问：** “父 Run 只剩两秒怎么办？”——先算 root/local 剩余 wall time；若 backoff 或下一 attempt 放不下，就不再 retry，外层 deadline 优先。

**第二轮追问：** “取消时正在 sleep？”——backoff 使用可取消的 async wait；Run 的 durable `CANCELLED` 优先于 pending retry。若整个进程死亡，恢复按 UTC `next_retry_at` 重算剩余等待，不复用跨进程无意义的 monotonic 值。

#### P0-8｜“at-least-once、at-most-once、exactly-once 放到 Tool 上怎么理解？”

**面试官为什么问：** 你解释副作用窗口后，分布式语义会自然被追问。

**先给结论：** at-least-once 倾向重试，保证至少尝试成功但可能重复；at-most-once 不重试未知调用，避免重复但可能丢失；exactly-once 需要业务幂等或原子协议，不能靠客户端口号得到。

**60～120 秒完整口语答案：** 对读取代码，at-least-once 重试通常可接受；对发送付款，未知时采用 at-most-once 可能避免重复，却可能漏执行。实际工程更常用 at-least-once delivery 加幂等消费，把重复请求映射为同一业务结果，得到“效果上的 exactly-once”。Axiom 的 stable invocation 与 ToolExecution 是本地基础；真正外部效果仍取决于 Tool 服务端契约。

**第一轮追问：** “为什么两阶段提交不解决？”——外部 Tool 往往不参与同一事务协调，2PC 成本与可用性代价也很高；幂等和补偿通常更现实。

**常见坑：** 声称网络系统能简单保证 exactly-once delivery。

**项目证据：** Durable Runtime retry safety 分支和 ToolExecution storage。

#### P0-9｜“Context、Memory、Checkpoint 有什么区别？为什么不能一直传全部历史？”

**面试官为什么问：** 简历同时写持久化与长上下文，容易把三者混成“记忆”。

**先给结论：** Context 是本次 LLM 真正看到的输入；Memory 是可跨 Turn 检索的事实/摘要；Checkpoint 是恢复执行的业务状态。全部历史会增加 Token、TTFT 和噪声，甚至超过模型窗口。

**60～120 秒完整口语答案：** Checkpoint 可以保存完整执行事实，但 ContextManager 只选择当前调用需要的投影；Memory 也要经过预算才注入。只保留最后 N 轮同样危险，因为早期目标和约束可能重要，而近期大 Tool 输出可能毫无价值。Axiom 保护系统协议、当前目标、近期原文和结构化任务状态，对较早内容摘要，对超大历史 Tool 结果投影，并在每次 LLM 前重新估算。执行正确性依赖的 pending Tool 不能只存在摘要里。

**第一轮追问：** “Context window 和 Run Token Budget 一样吗？”——前者限制一次模型请求能放多少输入/输出，后者限制整个 Run 累计资源；一个 Run 可多次都未超窗口但累计成本已超预算。

**常见坑：** 三者都叫 history；把长窗口等同于无需 Context 管理。

**项目证据：** `src/axiom/context.py`、`src/axiom/memory/`、`runtime/models.py`。

#### P0-10｜“Map-Reduce Summary 怎么做？哪些内容不能压缩，超大 Tool Result 怎么办？”

**面试官为什么问：** 简历点名 Map-Reduce，面试官会继续检查分段、协议原子性、超大结果和摘要幻觉。

**先给结论：** Map 阶段分别压缩较早历史，Reduce 阶段合并为结构化摘要；系统协议、当前目标/约束、开放任务、关键证据和 pending Tool 协议不能被静默删除。超大 Tool Result 的原始记录留在 durable evidence，只向模型投影有界关键片段。

**60～120 秒完整口语答案：** 长会话先按消息/协议边界分段，每段提取 objective、constraints、decisions、open tasks 和 evidence，再合并去重成较短 summary。Tool Call 与对应 Result 是协议单元，不能只保留一半；近期原文和 pinned state 优先保留。历史 Tool Result 即使十万 Token，也不删除持久原文，而是向 Context 投影名称、状态、关键头尾片段和裁剪标记。摘要可能遗漏或 hallucinate，所以它不是恢复事实；失败时原 Checkpoint 不变，若 pinned 内容仍超过 hard limit，就在 provider 调用前返回明确 ContextBudget 错误。

**第一轮追问：** “摘要 hallucinate 怎么办？”——固定结构字段、保留关键原文引用、确定性规则优先，并通过 retention/任务评测检测；不能把模型摘要当唯一事实。

**第二轮追问：** “为什么不直接保留最后 N 轮？”——轮数不等于 Token，一个 Tool Result 可能比几十轮更大；而旧目标和约束可能比近期噪声更重要，所以要按状态、协议和预算选择。

**常见坑：** “Map-Reduce 能无损压缩”；直接截字符串；把 durable Tool evidence 从持久层删除。

**项目证据：** `src/axiom/memory/summarizer.py`、`src/axiom/context.py`、context tests。

#### P0-11｜“Run 到底有哪些状态？从 RUNNING 到 COMPLETED 谁决定？”

**面试官想看什么：** 状态是否来自源码；终止提议与状态提交是否分开；非法转换怎样处理。

**先说结论：** 当前 `RunStatus` 是 `RUNNING / INTERRUPTED / WAITING_APPROVAL / WAITING_CHILD / COMPLETED / FAILED / CANCELLED`，没有持久化 `PENDING`。新 Checkpoint 直接从 `RUNNING` 开始；执行策略提出完成后，Runtime 还可能经过 Completion Verification，最后由版本化 Checkpoint 提交终态。

**60～120 秒完整口语答案：** “我会把状态机和协程分开讲。Run 一创建就是 `RUNNING`，遇到人工暂停是 `INTERRUPTED`，敏感 Tool 等批准是 `WAITING_APPROVAL`，父 Run 等 Child 是 `WAITING_CHILD`；`COMPLETED/FAILED/CANCELLED` 是终态。模型说 final 只是终止提议，如果配置了 Completion Contract，还要验证通过才提交 `COMPLETED`。API 先用 `allowed_operations` 做转换校验，Runtime 再用 sequence CAS 防旧状态覆盖新状态；所以最终依据是 durable transition，不是某个 Task 内存里的布尔值。”

**追问 1：resume 一个 COMPLETED Run？**

- **为什么问：** 看终态是否可逆。
- **30～60 秒回答：** “拒绝，API 返回 `invalid_run_transition`。`FAILED` 和 `CANCELLED` 也不能 resume；当前没有把 retry-failed-run 设计成原 Run 复活。”

**追问 2：cancel 一个 CANCELLED Run？**

- **为什么问：** 看控制幂等。
- **30～60 秒回答：** “Runtime 直接返回现有 CANCELLED，API 也允许 cancel；同 idempotency key 会重放原结果，不会再次执行取消副作用。”

**当前边界：** 状态校验与 CAS 已实现；没有通用外部工作流引擎，也没有 `PENDING/CANCELLING` 中间态。

#### P0-12｜“Checkpoint 为什么选语义边界，而不是每 token 或每 N 秒？”

**面试官想看什么：** 写放大、恢复价值和外部副作用窗口的真实取舍。

**先说结论：** 每 token 写入成本高且保存的是难以重放的半语义状态；固定定时器可能截在无意义中间态，也仍解决不了远端 Tool 歧义。LLM、Tool、interrupt、retry 等边界上的状态可重建、频率有界、恢复语义更容易解释。

**60～120 秒完整口语答案：** “每个 token checkpoint 会产生严重 write amplification 和存储量，而且半条 assistant message 通常不能从中间安全续写；定时 snapshot 看似均匀，但可能正好截在 JSON 参数、并发聚合或远端写操作中间，还需要和状态更新协调。语义边界的价值是：请求前后、Tool 状态、人工等待和终态都能定义下一步。但我不会说它消灭 crash window。LLM 响应返回后落盘前仍可能重请求；远端写成功、本地 `SUCCEEDED` 前仍是 unknown。”

**追问 1：那 checkpoint 越少越好吗？**

- **为什么问：** 看是否只会讲写放大。
- **30～60 秒回答：** “也不是。边界太稀会放大重做和成本，还可能重复副作用。选择标准是恢复价值，不是固定频率：昂贵请求、外部效果和控制状态最值得提交。”

**当前边界：** 当前源码在 Run start、LLM success/failure/retry、Tool result、interrupt/resume/cancel 等操作保存；没有增量 token checkpoint。

#### P0-13｜“你是不是通过 invocation ID 实现了 exactly-once Tool execution？”

**面试官想看什么：** 能否明确说 No，并区分身份、结果复用和下游幂等。

**先说结论：** No。稳定 invocation ID 只提供 logical invocation identity；`SUCCEEDED` 记录提供 Runtime-side completed-result reuse；只有下游接受幂等键、原子去重并可查询状态时，才能进一步约束重复业务效果。

**60～120 秒完整口语答案：** “Runtime 能保证的是：同一个 `run_id + tool_call_id` 得到稳定 invocation ID，同 ID 还要匹配参数哈希；如果 `SUCCEEDED` 已经持久化，恢复直接复用结果，不再调用 Tool。但是远端写成功以后、本地成功记录还没落盘就挂了，恢复只看到 `RUNNING`，结果是 UNKNOWN。invocation ID 不会自动跑到下游，也不会让两个系统原子提交，所以不能宣称 exactly-once side effect。”

**追问 1：Tool 成功但 ACK 丢了？**

- **为什么问：** 检查术语精度。
- **30～60 秒回答：** “这里要区分消息队列 ACK 和本地记录。Axiom 没用 Kafka/Redis Streams；准确说法是‘远端成功，但 Tool 结果成功持久化没有完成’。此时查下游 operation status；查不到且不是幂等写就人工确认或补偿。”

**追问 2：下游支持 idempotency key 和 status query 呢？**

- **为什么问：** 看生产协议如何闭环。
- **30～60 秒回答：** “把稳定业务 key 传给下游，下游用唯一约束绑定参数并对重复 key 返回同一 operation；timeout 后先查状态，再决定是否重发。这约束重复请求的业务效果，但仍不是网络只发送一次。”

**当前边界：** stable ID、参数哈希、成功结果复用和进程内 unsafe retry 停顿已实现；SQLite 重载会丢失 suppression reason，因此该停顿并非完整 restart-safe。通用下游状态查询/补偿协议也未实现。

#### P0-14｜“Run=INTERRUPTED 时 resume 和 cancel 同时发生，谁赢？”

**面试官想看什么：** race 的最终状态、CAS、live Task 与 durable transition 的顺序。

**先说结论：** 谁先成功提交合法的 durable transition，谁取得权威结果；另一方应看到冲突或终态。当前测试允许最终为 `COMPLETED` 或 `CANCELLED`，但不允许 stale writer 在取消后写回旧终态。

**60～120 秒完整口语答案：** “这不是固定规定 cancel 永远赢，而是竞争合法提交。resume 会把 `INTERRUPTED` 推到 `RUNNING` 并继续执行，cancel 会提交 `CANCELLED`。Checkpoint sequence CAS 防止双方都基于旧版本覆盖；执行循环也会 refresh 最新状态。若 resume 先完整完成，之后 cancel 对 COMPLETED 返回冲突；若 cancel 先提交，resume 的旧写失败或收敛到 CANCELLED。Supervisor 负责通知已经创建的本地 Task，但 Task 必须服从 durable 结果。”

**追问 1：会不会已经 resume 出 Task，又把 DB 写回 RUNNING？**

- **为什么问：** 看 Task 注册和状态 claim 的间隙。
- **30～60 秒回答：** “Task 注册不等于获得 durable ownership。后续保存仍带旧 sequence；如果 cancel 已写入新版本，resume claim 会冲突，supervise 路径读取到 CANCELLED 并返回，不允许 stale terminal overwrite。”

**当前边界：** 单进程锁、Supervisor、API conflict 与 SQLite CAS 已实现；不同控制意图仍是竞争语义，不是分布式优先级协议。

#### P0-15｜“父 Run cancel 后 Child Run 怎么处理？进程中途又挂了呢？”

**面试官想看什么：** lineage、传播顺序、已完成 Child、运行中 Tool 和 crash gap。

**先说结论：** Plan/Multi-Agent 当前先把父策略状态和父 Run 持久化为 CANCELLED，再遍历未终态 Child 调用 cancel；已完成 Child 保持终态。这个传播不是跨父子的一次原子事务，父落库后进程崩溃可能留下未取消 Child。

**60～120 秒完整口语答案：** “父 cancel 时，策略先把未完成 task/assignment 标成 cancelled，父 Checkpoint 落库，然后 `after_cancel` 读取每个 Child：终态的不改，非终态调用 Child Runtime cancel。本地活跃 Child 的 Task 会收到 Supervisor 信号；如果 Child 正在外部 Tool 中，只能阻止后续步骤，unsafe 外部副作用证据保持 UNKNOWN。若服务在父写完、子传播前崩溃，显式 Child resume/执行 preflight 会遍历 durable ancestors，发现 CANCELLED 后先把 Child 收敛为 CANCELLED，而不是运行。它不是原子级联，也不是后台扫描。”

**追问 1：子任务失败一定让父失败？**

- **为什么问：** 防止套用泛化 structured concurrency。
- **30～60 秒回答：** “不能一概而论，要看 Plan/Multi-Agent 策略如何记录 task/assignment、是否 replan/review、以及汇合规则。Child 独立失败是证据，父策略决定重试、重规划还是失败，不是 Python TaskGroup 的固定传播语义。”

**当前边界：** lineage、策略内传播、本地 Task cancel 和 resume/preflight 定点 reconciliation 已实现；自动 Recovery Scanner 与分布式级联事务未实现。

#### P0-16｜“服务重启后，谁知道哪些 RUNNING Run 要恢复？谁来接管？”

**面试官想看什么：** durable recovery capability 与 automatic ownership/failover 的边界。

**先说结论：** SQLite 仍由显式 resume 重建；PostgreSQL distributed Worker 的轮询 claim loop 会发现 runnable 且无有效 lease 的 `RUNNING` Run，原子取得更高 fencing token，再从最新 Checkpoint resume。`ActiveRunSupervisor` 仍不跨进程，只负责 owner 本地 Task。

**60～120 秒完整口语答案：** “服务重启后可以查到哪些 Checkpoint 仍是 RUNNING，但数据库记录不会自己执行。当前由客户端/API resume 触发，Runtime 先写一个 no-op claim checkpoint，让同一 sequence 的第二个恢复者冲突，再重建 LLM、Tool 和策略依赖。这个 claim 能减少双推进，却不是长期 owner lease，也不能保证两个 Worker 在 claim 前没调用外部 Tool。生产化我会用共享 PostgreSQL，原子抢 owner lease，heartbeat 续租，过期后 recovery scanner 接管，并用递增 fencing token 让旧 owner 的写失效。”

**追问 1：为什么 ActiveRunSupervisor 不是分布式调度器？**

- **为什么问：** 检查内存 registry 的作用域。
- **30～60 秒回答：** “它保存本进程 Task 和 Event Loop 引用，用于线程安全 cancel 和 drain；进程一死引用全没，也没有跨节点共识或租约。所以它是 live execution registry，不是 durable ownership service。”

**当前边界：** PostgreSQL 的 claim/lease/heartbeat/fencing 与过期自动接管已实现；全局准入、背压、公平/优先级调度、Run DLQ 和跨进程即时 Task signal 仍未实现。

### P1 — 回答后的自然深挖（8 题）

#### P1-1｜“Run Budget 和 Context Budget 有什么区别？”

**为什么会被追到这里：** 你在停止条件和上下文回答中主动提到两种预算。

**30～60 秒回答：** Context Budget 控制一次 LLM 请求放入多少内容；Run Budget 控制整个 Run 的累计步骤、调用、Token、时间和成本。前者保护窗口与单次延迟，后者防长任务累计失控。

**典型继续追问：** “哪个先检查？” **回答思路：** 每次调用前分别完成上下文组装和资源预检。**项目边界：** 两者有关联但不是同一个计数器。

#### P1-2｜“为什么预算要调用前检查？输出 Token 又不知道。”

**为什么会被追到这里：** 你说预算是硬治理。

**30～60 秒回答：** 只事后统计会在并发 Child 中超卖。调用前按请求上限或估算预留，完成后用 provider usage 核销，多退少补；若未知则保留估算/unknown，不伪造精确值。

**典型继续追问：** “预留太保守怎么办？” **回答思路：** 用历史分位数和最大输出上限调节，同时保证硬上限。**项目边界：** 成本依赖已知定价，未知价格不是 0。

#### P1-3｜“Child Run 怎么共享 Parent 预算？”

**为什么会被追到这里：** 你讲到 Plan/Multi-Agent 恢复。

**30～60 秒回答：** 每个 Child 记录自身使用，但放行和聚合落到 root owner ledger；版本化 CAS 避免多个 Child 同时看到余额后各自花满。

**典型继续追问：** “只用 Semaphore 不行吗？” **回答思路：** Semaphore 控同时执行数，不控制累计 Token/成本。**项目边界：** 当前是本地共享 ledger，不是跨集群全局配额。

#### P1-4｜“max_steps 为什么不能解决死循环？”

**为什么会被追到这里：** 你说 ReAct 会被预算终止。

**30～60 秒回答：** max_steps 只能规定最坏上限，无法判断是否仍有新证据。ProgressDetector 比较动作、错误、短周期和状态指纹，先给恢复提示，继续无进展才终止。

**典型继续追问：** “重复同一个 Tool 一定无进展？” **回答思路：** 不一定，状态/证据变化时可能合理。**项目边界：** 检测是规则治理，不是通用任务正确性证明。

#### P1-5｜“SQLite 的 WAL 和事务在这里解决什么？”

**为什么会被追到这里：** 你解释了 Checkpoint CAS。

**30～60 秒回答：** WAL 改善读写并存和崩溃恢复，事务保证版本检查与写入原子；但 SQLite 仍有单写者竞争。Child Run 增多、事务变长或 checkpoint 频繁时写等待会上升。

**典型继续追问：** “加连接池能解决吗？” **回答思路：** 不能绕过数据库写容量，过多连接反而增加竞争。**项目边界：** 当前适合单 Runtime。

#### P1-6｜“摘要失败或 Token 估算不准怎么办？”

**为什么会被追到这里：** 你提到模型摘要和预估。

**30～60 秒回答：** summary failure 不能覆盖原状态；保留原 Checkpoint，尝试规则投影或返回明确错误。估算值要留安全余量，每次调用前重估，provider 返回真实 usage 后再核销。

**典型继续追问：** “能不能直接调用看看？” **回答思路：** pinned-only 已超硬限时必须在调用前 fail closed。**项目边界：** 目标压缩比不是每次严格收敛保证。

#### P1-7｜“两个 Worker 同时推进同一个 Run，会怎样？”

**为什么会被追到这里：** 你解释 CAS 后自然产生所有权问题。

**30～60 秒回答：** PostgreSQL 用短事务原子 claim，所以同一代只有一个 Worker 获得 lease；所有执行权威写同时校验 fencing token。Checkpoint CAS 仍负责 sequence 冲突。即便如此，已发出的外部调用仍可能产生副作用，所以还需要稳定 invocation 与下游幂等，不能宣称 exactly-once。

**典型继续追问：** “fencing 是什么？” **回答思路：** 每次 claim 递增所有权令牌；Checkpoint、ToolExecution 与 budget ledger 写在同一短事务中锁定并校验当前 owner/token/未过期 lease，旧令牌写抛出 `OwnershipLostError`。

#### P1-8｜“CAS 和幂等为什么不是一回事？”

**为什么会被追到这里：** 你同时提到了版本和 stable invocation。

**30～60 秒回答：** CAS 检查状态版本，防止旧更新覆盖新状态；幂等保证同一业务操作重复提交仍得到同一效果。一次 resume 可用 CAS 防冲突，Tool 写副作用仍要 invocation/idempotency key 去重。

**典型继续追问：** “两者是否都需要？” **回答思路：** 有并发状态更新和外部副作用时通常都需要。**项目边界：** 外部幂等取决于 Tool 服务。

### P2 — 攻击、边界与架构取舍（4 题）

#### P2-1｜“你怎么证明上下文压缩没有让 Agent 变笨？”

**最安全的结论：** 压缩比和结构单测不能证明任务语义无损。当前已经分四层取证：结构/协议不变量、声明式 required-state retention、压缩比例，以及适用于确定性任务的 full-context 与 compressed-context 结果对照；但仍不声称开放域语义等价。

**回答主线：** 仓库现在有 10 个固定离线 Context Retention Case，覆盖远端旧目标、多约束、open task、decision、artifact、早期失败、超大 Tool 输出、协议配对、previous summary、新噪声和 pending Tool。每个 required item 做确定性匹配，输出 before/after Token、compression ratio、retention rate 与 protocol integrity；可选 outcome probe 对同一合成任务跑 full/compressed 两份上下文。当前固定策略实测保留 22/22 项、10/10 协议有效，平均 after/before 为 0.5348；这只代表该合成集。baseline/candidate 中 retention 或协议下降是硬回归，压缩变差默认只告警，压得更多不能抵消状态丢失。

**不要说什么：** “压缩率高就证明更好”“单测通过所以一样聪明”，也不要编当前不存在的成功率。

**当前限制：** 固定 golden set 只能证明声明的事实和确定性任务结果，没有 LLM Judge，也不能证明任意自然语言摘要的语义等价。ContextManager 尚无逐阶段 provenance，因此无法精确归因时统一报告 `lost_after_compaction`。

#### P2-2｜“你说支持重启恢复，是不是等于 exactly-once？”

**最安全的结论：** 不是。恢复与本地去重缩小重做范围，外部写仍有未知窗口。

**回答主线：** 分清 Checkpoint、ToolExecution、外部系统三方无法原子提交；明确 read/idempotent/non-idempotent 三类处理。

**不要说什么：** “稳定 invocation ID 自动让所有服务幂等。”

**如果继续扩展怎么做：** Tool capability contract、服务端唯一键、operation status、补偿和人工状态。

#### P2-3｜“SQLite 能支撑多个 Runtime 共同恢复吗？”

**最安全的结论：** 当前边界主要是本地/单 Runtime，不能把 WAL 和 CAS 外推为共享调度数据库。

**回答主线：** 多实例需要共享 PostgreSQL、所有权租约、心跳、fencing、连接容量和事件一致性。

**不要说什么：** “挂共享磁盘就能安全水平扩展。”

**如果继续扩展怎么做：** 先迁移共享 durable state，再增加原子 claim，而不是先扩 Worker。

#### P2-4｜“模型提出 final，但 Completion Verification 失败时，Budget、Progress 和 deadline 谁说了算？”

**最安全的结论：** verifier 不能绕过已有控制。ReAct 只在契约允许时获得一次有界结构化纠正，下一轮仍照常消费 step、model call、Token、cost 和 wall time；任何外层预算、deadline、cancel 或 no-progress 先到都保持权威。

**回答主线：** 区分 termination proposal、Completion Contract 与最终 Run 状态。通过是 VERIFIED；失败是 NOT_VERIFIED，允许的一次修正会回到普通 loop；再次失败为 `COMPLETION_NOT_VERIFIED`。没有契约的开放任务兼容完成但 verification 为 NOT_APPLICABLE。Plan/Multi-Agent 当前在终止点只验证一次。

**不要说什么：** “verifier 会无限要求模型修正”“为了完成验证可以额外免费调用模型”“没有 contract 也能证明开放任务正确”。

**当前边界：** deterministic checks 只证明已编码的 Run/Tool/output/artifact/Plan/Child 条件，不是通用语义正确性或 LLM Judge。

## 10. 重点追问树（11 条）

### 追问树 1：状态模型

```text
Thread / Turn / Event / Run / Checkpoint 为什么拆？
└─ Event 和 Checkpoint 有什么本质区别？
   └─ Memory、Context、Checkpoint 又怎么分？
      └─ 两类持久记录不一致时信谁？
```

- **“Event 和 Checkpoint 区别？”**
  - **面试官意图：** 检查事件历史与恢复快照是否重复建模。
  - **回答思路：** Event 回答发生过什么、支持重放；Checkpoint 回答下一步从哪里推进。
  - **30～60 秒口述：** “Event 是追加的交互/可见事实，适合 SSE 按 ID 重放；Checkpoint 是某个 Run 的最新可恢复状态，包含 pending Tool、策略状态、版本和错误。只重放 Event 恢复状态机复杂，只有 Checkpoint 又缺完整历史，所以职责分开。”
- **“Memory、Context、Checkpoint 怎么分？”**
  - **面试官意图：** 排除把所有 history 都叫记忆。
  - **回答思路：** Memory 跨回合检索，Context 是一次模型投影，Checkpoint 是执行事实。
  - **30～60 秒口述：** “Memory 保存可跨 Turn 使用的事实/摘要；Context 是这一次 LLM 实际看到的有界输入；Checkpoint 保存恢复执行必须的业务状态。Memory 和历史都要经过 Context Budget 才能入模，Context 被压缩也不能删除 Checkpoint 真相。”
- **“不一致时信谁？”**
  - **面试官意图：** 追问双写边界。
  - **回答思路：** 恢复以 Checkpoint 为权威，Event 用于展示；生产可用事务/outbox 缩小窗口。
  - **30～60 秒口述：** “推进 Run 时以版本化 Checkpoint 为恢复事实，Event 不应反向驱动未验证的状态迁移。若 Checkpoint 已提交而展示事件未写，可能少一条 UI 记录；反过来更危险。当前按实际事务边界处理，服务化可用同事务或 outbox 改善一致性。”

### 追问树 2：保存边界

```text
为什么在 LLM / Tool / interrupt 边界保存？
└─ 为什么不每个 token 都 checkpoint？
   ├─ LLM 完成但本地保存失败怎么办？
   └─ Tool 成功但结果保存失败怎么办？
```

- **“为什么不每 token 保存？”**
  - **面试官意图：** 检查 durability 与写放大的取舍。
  - **回答思路：** 选择语义稳定、昂贵或有副作用的边界；半 token 难续接。
  - **30～60 秒口述：** “每个 token 写 SQLite 会放大 I/O 和竞争，而且半条生成通常不能安全恢复。LLM 前后、Tool 前后、interrupt/HITL 是语义稳定边界：要么昂贵、要么有副作用、要么改变控制状态，保存它们能最大程度减少重做。”
- **“LLM 完成但保存失败？”**
  - **面试官意图：** 检查模型调用 crash window 和成本诚实度。
  - **回答思路：** 从上个边界恢复可能重做；没有外部写但会重复成本；未知 usage 不伪造。
  - **30～60 秒口述：** “若响应已返回但 Checkpoint 没落盘，恢复只能看到上个边界，可能重新请求模型。它通常没有外部业务副作用，但会增加调用和成本；Provider 已报告的部分 usage 要保留，未报告的崩溃窗口不能凭空恢复成精确数字。”
- **“Tool 成功但保存失败？”**
  - **面试官意图：** 这是 exactly-once 攻击点。
  - **回答思路：** 外部效果未知；读取/幂等才可自动重试，普通写等待确认。
  - **30～60 秒口述：** “执行前先写 ToolExecution RUNNING，成功后写 SUCCEEDED。若远端已写成功而本地仍 RUNNING，恢复时结果只能是 unknown。读或服务端幂等操作可重试；普通写需要 operation status、人工确认或补偿，不能把网络 timeout 当作未执行。”

### 追问树 3：CAS

```text
Checkpoint 为什么需要 CAS？
└─ 有事务为什么还要版本号？
   └─ CAS、锁、幂等分别解决什么？
      └─ CAS 能防两个 Worker 同时执行 Tool 吗？
```

- **“事务为什么不够？”**
  - **面试官意图：** 追问 optimistic concurrency 基础。
  - **回答思路：** 事务保证本次原子，不知道调用者读取版本是否陈旧；CAS 编码前置条件。
  - **30～60 秒口述：** “两个执行者都读到 v5，各自事务都可以合法写入；若后写者不检查版本，就会覆盖基于旧状态算出的结果。CAS 在更新里要求当前仍是 v5，成功者变 v6，另一个冲突并重载，解决 lost update。”
- **“CAS、锁、幂等怎么分？”**
  - **面试官意图：** 检查三个高频词是否混用。
  - **回答思路：** CAS 拒绝 stale write；锁阻止并发进入；幂等保证重复业务操作同效果。
  - **30～60 秒口述：** “进程内锁减少同一 Run 同时推进；CAS 在存储层保护版本，即使锁丢失仍能拒绝旧写；幂等处理网络重发或恢复重试，让同一业务操作不重复产生效果。三者可以同时需要，任何一个都不能替代另外两个。”
- **“能防双执行 Tool 吗？”**
  - **面试官意图：** 从状态一致性追到所有权。
  - **回答思路：** CAS 只能让最终状态一个写赢，外部调用可能已发出；多机需 lease/claim/fencing。
  - **30～60 秒口述：** “不能。两个 Worker 可能都在 CAS 前调用了外部 Tool，之后只有一个 Checkpoint 写成功，也已经产生双副作用。当前用进程内锁/Supervisor 控本地 ownership；多进程需要原子 claim、lease 和 fencing token，CAS 不等于 ownership。”

### 追问树 4：Tool 恢复

```text
stable invocation ID 解决什么？
└─ logical Tool invocation 与 Tool attempt 有何区别？
   └─ retry 第二次后 crash 会不会从零开始？
      └─ 有 idempotency key 就 exactly-once 吗？
```

- **“invocation 和 attempt 怎么分？”**
  - **面试官意图：** 检查 retry 记账和恢复身份。
  - **回答思路：** 一个模型 Tool Call 是逻辑 invocation；每次真实执行是 attempt，共用稳定 ID。
  - **30～60 秒口述：** “同一 Tool Call 恢复或重试时逻辑身份不变，参数 hash 也必须一致；attempt 1、2、3 是对依赖的实际执行。ToolExecution 把它们挂在同一 invocation 下。预算的 Tool call 计数按真实 attempt 消耗，retry_count 只数首个之后的尝试。”
- **“第二次后 crash 呢？”**
  - **面试官意图：** 追问 durable retry allowance。
  - **回答思路：** SQLite 已持久化 attempt、显式 retry state 与 UTC deadline。
  - **30～60 秒口述：** “恢复读取同一 ToolExecution，attempt 不会回到 0，达到最大次数会保持 RETRY_EXHAUSTED，SUCCEEDED 直接复用；RETRY_PENDING 会按 UTC `next_retry_at` 继续等待，unsafe UNKNOWN 保持 RETRY_SUPPRESSED。”
- **“幂等键等于 exactly-once？”**
  - **面试官意图：** 捕捉分布式语义夸大。
  - **回答思路：** 只有服务端接收、持久化、唯一约束并对同 key 返回同结果才有效。
  - **30～60 秒口述：** “本地 stable ID 只是提供候选 key。服务端必须把 key 与业务参数绑定并原子去重，客户端还要处理状态查询；即使如此，更准确的说法是重复请求得到同一业务效果，不是网络只投递一次。没有服务端契约仍是 ambiguous。”

### 追问树 5：Map-Reduce Context

```text
为什么不直接保留最后 N 轮？
└─ Token 数与轮数为什么不同？
   └─ pinned state 和 Tool protocol 怎么保护？
      └─ summary hallucination / hard limit 怎么办？
```

- **“轮数为什么不够？”**
  - **面试官意图：** 看 Context 控制是否理解真实 Token 风险。
  - **回答思路：** 一轮可能是十万 Token Tool 输出；旧约束比新噪声重要。
  - **30～60 秒口述：** “最近 10 轮可能包含一个超大日志，远比 100 个短消息贵；旧目标和约束又可能必须保留，所以 round-based window 同时可能过大和丢关键状态。Axiom 每次按估算 Token 重组，而不是机械按轮数截断。”
- **“pinned 与协议怎么保护？”**
  - **面试官意图：** 追问哪些状态不能被摘要替代。
  - **回答思路：** 当前 objective/constraints/open work、system protocol、pending Tool；Tool Call/Result 原子分组。
  - **30～60 秒口述：** “Pinned state 是当前任务不能随旧历史淘汰的目标、约束、开放任务和协议状态。Tool Call 与对应 Result 必须作为 message unit 一起保留或一起投影，不能只删一半。历史大结果可投影，但 durable 原文不删除。”
- **“摘要错或仍超限？”**
  - **面试官意图：** 检查 fail-open 风险。
  - **回答思路：** 摘要非事实源；结构字段/近期原文兜底；pinned 超 hard limit 时 provider 前失败。
  - **30～60 秒口述：** “摘要可能遗漏或 hallucinate，所以它不能覆盖原 Checkpoint；保留结构字段、近期原文和关键引用，并用 retention benchmark 检查。压缩后重估，若固定内容本身仍超 hard limit，就返回 `CONTEXT_BUDGET_EXCEEDED`，不能偷偷删目标后调用模型。”

### 追问树 6：预算与进展

```text
Context Budget 与 Run Budget 有何区别？
└─ Child Run 并发如何避免预算超卖？
   └─ timeout 与 deadline 有何区别，retry 免费吗？
      └─ completion correction 会绕过 Budget/Progress 吗？
```

- **“Child 怎么避免超卖？”**
  - **面试官意图：** 从累计预算追到并发账本。
  - **回答思路：** Child 本地使用归集 root ledger，调用前原子预留，完成后按 actual usage 核销。
  - **30～60 秒口述：** “每个 Child 有自身计数，但是否放行要看同一 root owner 的聚合账本。多个 Child 在真实调用前通过版本化更新预留，结束后用 Provider usage 核销，避免每个 Child 都认为自己还有全额 Token 或 cost。”
- **“timeout/deadline/retry 怎么算？”**
  - **面试官意图：** 检查依赖控制与外层 lifetime。
  - **回答思路：** timeout 限单 attempt；deadline 限 Run；effective timeout 取 min；真实 attempt 计费。
  - **30～60 秒口述：** “Tool 配 10 秒、Run 只剩 2 秒时，有效 timeout 最多约 2 秒。若下一 backoff 要 4 秒就不 retry。每个实际 model request 或 Tool attempt 都占普通调用和 wall/token/cost 账本；只有恢复复用已成功结果不重复收费。”
- **“completion correction 会绕过吗？”**
  - **面试官意图：** 验证新增 verifier 没有隐藏无限 loop。
  - **回答思路：** 一次结构化反馈回到普通 ReAct；下一步照常预算和 no-progress。
  - **30～60 秒口述：** “不会。Verifier 失败时 ReAct 最多按 contract 给有界反馈，随后仍是普通 step/model call，取消、wall deadline、Token/cost 和 ProgressDetector 都保持权威。再次不通过则 `COMPLETION_NOT_VERIFIED`，Plan/Multi-Agent 当前只做一次终止验证。”

### 追问树 7：压缩质量攻击

```text
你怎么证明 Context 压缩没让 Agent 变笨？
└─ 结构测试和 compression ratio 为什么不够？
   └─ 当前 10-case benchmark 到底测了什么、结果多少？
      └─ 这些数字能证明通用语义等价吗？
```

- **“为什么压缩率不够？”**
  - **面试官意图：** 防止把效率指标冒充质量。
  - **回答思路：** 压得多可能删错；分开测协议、required state、压缩量和任务结果。
  - **30～60 秒口述：** “compression ratio 只回答输入缩小多少，完全不说明删掉的是噪声还是约束。Axiom 分四层看：消息/Tool 协议结构、声明的 required-state retention、before/after Token 与 ratio，以及适用于确定性任务的 full/compressed outcome 对照。”
- **“10-case 测什么、结果多少？”**
  - **面试官意图：** 要求当前可复现证据而非设计愿景。
  - **回答思路：** 旧目标、多约束、open/completed、失败证据、超大 Tool、协议、previous summary、噪声、pinned/restart。
  - **30～60 秒口述：** “固定 10 个合成 case 覆盖旧 objective、多 constraints、开放任务、decision、artifact、关键错误、超大 Tool 投影、Tool pair、已有 summary 与噪声等。当前策略保留 22/22 required items，10/10 protocol-valid，平均 after/before ratio 是 0.5348，也就是估算 Token 减少约 46.5%。”
- **“能证明通用语义等价吗？”**
  - **面试官意图：** 检查限制声明。
  - **回答思路：** 不能；固定 synthetic golden 只证明声明事实和可确定 outcome，无 LLM Judge。
  - **30～60 秒口述：** “不能。它证明当前固定合成集的 required items 和协议没有丢，并能在少量确定性 probe 对比结果；开放域摘要、隐含语义和真实模型推理仍可能受损。Retention 或协议下降是硬回归，压缩变差只告警，但这仍不是 universal semantic equivalence。”

### 追问树 8：Run 状态与控制竞争

```text
Run 有哪些真实状态？
└─ resume COMPLETED / CANCELLED 为什么拒绝？
   └─ INTERRUPTED 上 resume 与 cancel 同时来，谁赢？
      └─ live Task 已创建后，怎样避免把 DB 写回旧状态？
```

- **“resume 与 cancel 谁赢？”**
  - **为什么问：** 看状态机、API 幂等、CAS 和 Task 是否形成一套语义。
  - **30～60 秒回答：** “没有硬编码永远谁优先。谁先提交合法 durable transition 谁生效；另一方拿到冲突或最新终态。当前测试允许最终 COMPLETED 或 CANCELLED，但 sequence CAS 和 refresh 不允许取消之后再被 stale completion 覆盖。”
- **“Task 已经启动呢？”**
  - **为什么问：** 检查 durable truth 与 live execution 分离。
  - **30～60 秒回答：** “注册 Supervisor 只表示本地有 Task，不表示拿到永久所有权。cancel 先落库后会通知 Task；Task 下一次 checkpoint 若还是旧 sequence 会冲突并读取 CANCELLED。durable state 权威，Task 跟随它。”

### 追问树 9：Crash window 与 exactly-once

```text
Tool 成功但 Checkpoint 前挂了怎么办？
└─ SUCCEEDED 已落库和仍 RUNNING 有何不同？
   └─ invocation ID 为什么仍不够？
      └─ status query / idempotency key / 人工介入怎么选？
```

- **“SUCCEEDED 已落库？”**
  - **为什么问：** 区分 ToolExecution 与父 Checkpoint 两层证据。
  - **30～60 秒回答：** “如果 ToolExecution 已是 SUCCEEDED，即使父 Checkpoint 还没推进，也能用 invocation ID 和参数哈希复用结果；如果仍是 RUNNING，远端可能成功也可能没执行，不能自动当失败。”
- **“什么时候人工介入？”**
  - **为什么问：** 看未知副作用是否被安全收敛。
  - **30～60 秒回答：** “普通外部写既没有幂等 key，也不能查询 operation status，又无法可靠补偿时，Runtime 只能停在 WAITING_APPROVAL 让人确认。自动重试是在制造重复，不是恢复。”

### 追问树 10：Parent / Child 取消

```text
父 Run cancel 后 Child 怎么办？
└─ 已完成 Child 与活跃 Child 分别怎样处理？
   └─ Child 正在外部 Tool 中怎么办？
      └─ 父已 CANCELLED、传播前 crash 怎么收敛？
```

- **“当前传播顺序？”**
  - **为什么问：** 检查源码而不是套用 structured concurrency。
  - **30～60 秒回答：** “Plan/Multi-Agent 先更新父策略状态并保存父 CANCELLED，再遍历非终态 Child 调用 cancel；已完成 Child 不回滚。活跃本地 Task 会收到 Supervisor 信号，外部副作用不保证撤销。”
- **“传播中 crash？”**
  - **为什么问：** 暴露跨 Run 原子性边界。
  - **30～60 秒回答：** “父和所有 Child 不在一个事务里，所以会出现父已取消、部分 Child 未取消。当前显式 Child resume/执行 preflight 会检查 durable ancestors 并收敛取消；没有后台 Scanner，因此无人触发的遗留 Child 不会自动被扫描。”

### 追问树 11：重启接管与 SQLite

```text
服务重启后谁接管 RUNNING Run？
└─ resume.claim 是否就是分布式锁？
   └─ SQLite WAL 能否支撑多实例？
      └─ PostgreSQL + lease + heartbeat + fencing 怎样分工？
```

- **“resume.claim 是锁吗？”**
  - **为什么问：** 防止把一次 CAS 外推成持续 ownership。
  - **30～60 秒回答：** “它是对当前 checkpoint sequence 的乐观 claim，只能让两个恢复者不能都从同一版本继续写；它没有租期、续租和 owner identity，也不能阻止 claim 前的外部调用，所以不是分布式锁。”
- **“SQLite 为什么现在合适、何时迁 PostgreSQL？”**
  - **为什么问：** 简历明确写 SQLite。
  - **30～60 秒回答：** “项目先服务本地 Runtime，SQLite 零运维、事务和 WAL 适合小写并发与快速迭代，所以保留为默认。PostgreSQL 先提供共享真相和跨进程 CAS，现在又加入 runnable claim、lease、heartbeat 和 fencing；两个并发 claimer 只有一个获得当前执行权，旧 owner 的权威写会被拒绝。”
- **“为什么 durable truth 不只放 Redis？”**
  - **为什么问：** 区分正确性真相与临时协调缓存。
  - **30～60 秒回答：** “Run、Checkpoint 和 ToolExecution 决定崩溃后能否安全恢复，需要事务、约束、可查询历史和明确 schema；PostgreSQL 是 durable correctness truth。Redis 以后可以辅助通知或协调，但不能在当前设计里成为唯一真相源。”

## 11. 面试官攻击面与防守口径

1. **“对象太多是过度设计？”** 用交互、重放、执行和恢复职责拆分回答。
2. **“存 messages 就够？”** 举 pending Tool 与 DAG 状态反例。
3. **“CAS 就能防双执行？”** 明确状态冲突与所有权不同。
4. **“恢复就是 exactly-once？”** 主动讲外部副作用 unknown window。
5. **“长窗口还要压缩？”** 从成本、TTFT、噪声和累计历史回答。
6. **“Map-Reduce 无损吗？”** 明确摘要风险和原始 durable evidence。
7. **“压缩后没变笨？”** 展示结构/协议、required-state retention、压缩比和确定性 A/B 四层证据，同时明确不证明开放域语义等价。
8. **“预算事后统计就行？”** 并发 Child 会超卖，必须预留/核销。

## 12. 当前边界

### 【当前已实现】

SQLite/Memory Runtime store、可选 PostgreSQL shared durable store 与 bounded pool、两后端 Checkpoint CAS/ToolExecution/Event/control schema parity、LLM/Tool/interrupt 边界、stable invocation、ToolExecution 成功复用、attempt/retry state 持久化、分层 timeout/deadline、安全 retry 与 full jitter、ReAct 进程重启恢复、上下文高水位/目标/硬上限、Tool 投影、Map-Reduce 会话摘要、10-case Context Retention 离线评测、根 Run 预算账本、无进展检测。

### 【当前部分支持】

外部写副作用只能在有幂等契约时安全重试；两种 durable backend 都持久化显式 retry state、`next_retry_at`、失败分类和 suppression reason，但 ToolExecution 唯一约束与 Runtime fencing 仍不能证明外部 exactly-once；PostgreSQL storage contract 与 ownership matrix 已在真实 PostgreSQL 15 上通过，普通 CI 尚未配置 PostgreSQL 服务；Context retention 能验证声明事实但没有证明开放域语义等价；模型价格可能未知；本地 Supervisor 不做跨进程 ownership，自动接管由 PostgreSQL claim/lease/fence Worker loop 完成。

### 【未来可扩展】

Durable Run Queue、Run lease/heartbeat/fencing、幂等 Tool contract、语义级 Context 质量评测、按租户全局预算、自动 Recovery Scanner/Worker 接管和跨实例事件总线。

## 13. 面试前 5 分钟速背

### 5 个概念

Checkpoint；CAS；stable invocation；Context projection；root Run budget/no-progress。

### 5 个源码锚点

`runtime/durable.py`；`runtime/checkpoints.py`；`context.py`；`runtime/budget.py`；`runtime/progress.py`。

### 必须不思考就能回答的 5 道 P0

怎样恢复？Tool 半成功怎么办？三种状态数据有何区别？怎样证明压缩质量？如何阻止死循环？

### 3 个陷阱

不承诺外部 exactly-once；不把 CAS 当租约；不拿压缩比冒充任务质量。

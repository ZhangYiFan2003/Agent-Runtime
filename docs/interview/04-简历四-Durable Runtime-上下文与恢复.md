# 简历四：Durable Runtime、上下文与恢复

> 本册回答“长任务怎样保存、恢复、控成本、避免死循环，以及上下文如何不失控”。

## 0. 简历原句

> 基于 SQLite 持久化 Thread / Turn / Event、Run / Checkpoint 与 ToolExecution，在 LLM / Tool / interrupt 边界保存状态，支持 ReAct Run 进程重启恢复并通过稳定 invocation ID 复用已完成 Tool 结果，结合 Map-Reduce 摘要与 Token Budget 管理长上下文。

## 1. 这条经历解决了什么问题

Agent 任务比普通 HTTP 请求更长，也更有状态。一次任务可能经历多轮模型调用、Tool 副作用、人工审批和 Child Run；如果所有状态只在协程栈和消息列表里，进程退出后只能从头执行，既浪费 Token，也可能重复外部写操作。

另一个问题是上下文会不断增长。直接保留全部消息会提高成本和模型首字延迟，甚至超过窗口；粗暴截断又可能丢掉目标、约束、尚未完成的工作和 Tool 协议配对。Axiom 把执行持久化、Tool 去重、上下文压缩、预算和无进展检测作为一组长期运行治理能力。

## 2. 30～45 秒主回答

我把 durable Run 看成一个持久化状态机。Runtime 在 LLM、Tool、interrupt 等关键边界保存带版本号的 Checkpoint，并用 CAS 拒绝旧版本覆盖新状态；Tool 调用使用稳定 invocation ID，成功结果写入 ToolExecution，恢复时相同调用可直接复用。

长上下文由 ContextManager 在每次模型调用前重新估算：保留系统协议、当前目标、近期原始消息和结构化任务状态，对较早内容做摘要，对过大的历史 Tool 输出做投影，并在硬上限前 fail closed。RunBudget 同时限制步骤、模型/工具调用、Token、时间和成本；ProgressDetector 检查重复动作、重复错误、短循环和状态停滞。当前保证的是本地 SQLite 上的恢复和治理，不是外部写操作的 exactly-once，也不是分布式 Worker 接管。

## 3. 2～3 分钟完整故事

我先把会话和执行状态分开。Thread/Turn/Event 是用户交互与事件历史；Run/Checkpoint 是可恢复执行状态；ToolExecution 是外部调用账本。这样 SSE 重放、对话恢复和 Agent 执行恢复不会混成一个表，也能分别定义一致性边界。

每个 Checkpoint 有版本号。保存时使用 compare-and-swap（CAS，比较并交换）：只有数据库中的版本等于调用方预期版本，才允许写入新版本。SQLite 实现用事务和 `BEGIN IMMEDIATE` 限制写竞争。它能拒绝过期执行者的旧状态，但在单进程内还配合 Run 锁；到了多进程，还要新增租约和 fencing token，CAS 本身不等于所有权。

Tool 恢复是最需要诚实的地方。稳定 invocation ID 由 Run 与 Tool call 身份派生，并保存参数哈希。若记录已是 `SUCCEEDED`，恢复时可以复用结果；若进程在 Tool 外部副作用成功后、写成功记录前崩溃，数据库里可能仍是 `RUNNING`，此时结果未知。读操作或带幂等键的 Tool 可以安全重试，其他写操作需要查询服务端状态、人工处理或补偿。

上下文管理不是简单“超过长度就 summary”。ContextManager 先组成消息单元，保护 tool_call/tool_result 配对与最新用户目标，复用已有结构化摘要，将历史超大 Tool 输出投影为名称、状态和裁剪内容。达到高水位才压缩到目标区间；若固定内容本身超过硬上限，在调用 LLM 之前就失败。Map-Reduce 摘要也用于长会话记忆，但执行态摘要和长期事实不能混为一谈。

最后用预算、deadline 和无进展检测封顶。预算在调用前预留、调用后按实际用量核销，Child Run 消耗归集到 root ledger；未知模型价格标成 unknown，而不是按 0 元。LLM/Tool attempt 的 timeout 会取配置值和 root Run 剩余 lifetime 的较小值；暂态失败的每次 retry 仍占普通调用预算。ProgressDetector 使用稳定指纹识别同动作、同错误、2～4 步短循环和状态无变化，先给恢复提示，仍不收敛则以 `NO_PROGRESS` 终止。

## 4. 架构与调用链

```text
Thread → Turn → Event（交互与重放）
                 │
                 ↓
Run → versioned Checkpoint ← CAS
 │       │      │
 │       │      └─ strategy/context/progress/budget state
 │       └─ LLM / Tool / interrupt 边界保存
 └─ ToolExecution(invocation_id, args_hash, status, result)

每次 LLM 前：加载状态 → ContextManager 投影 → Budget 预检
执行后：实际用量核销 → ProgressDetector → 保存新 Checkpoint
```

## 5. 最新源码实现

- `src/axiom/runtime/models.py`：`RunStatus`、`Checkpoint`、`ToolExecutionRecord`、`BudgetLedgerRecord`。
- `src/axiom/runtime/checkpoints.py`：Memory/SQLite store、Checkpoint CAS、ToolExecution 和版本化 budget ledger；SQLite 开 WAL、`busy_timeout=30s`。
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

## 9. 分级面试题库（22 题）

### P0 — 简历直击题（10 题）

#### P0-1｜“为什么分 Thread、Turn、Event、Run、Checkpoint 这么多对象？”

**面试官为什么问：** 简历一次列出五类状态，面试官会检查它们是否只是重复建模。

**先给结论：** Thread/Turn/Event 描述用户交互历史，Run/Checkpoint 描述可恢复执行。拆开后，一个用户回合可以拥有父子多个 Run，事件用于重放，Checkpoint 用于恢复，不会互相污染职责。

**60～120 秒完整口语答案：** Thread 是会话容器，Turn 是一次用户输入到系统响应的交互，Event 是其中可追加、可按 ID 重放的事实。Run 是一次可控制的 Agent 执行，复杂模式下还会有 Child Run；Checkpoint 是 Run 在某个安全边界上的最新可恢复状态。Event 适合展示“发生过什么”，但仅靠事件重放恢复状态机复杂且容易漏不变量；Checkpoint 直接保存“下一步从哪里继续”。反过来，Checkpoint 也不适合替代完整用户事件历史。

**第一轮追问：** “创建 Turn 后为什么还要 Run？”——Turn 面向交互，一个 Turn 可能触发父 Run 和多个 Child Run；Run 还需要独立状态、预算、取消和 Trace。

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

**60～120 秒完整口语答案：** Runtime 不恢复原协程栈，而是重新构造 DurableAgentRuntime，读取 Checkpoint 中的消息、策略状态、pending Tool、interrupt 和版本。如果处于可恢复状态，`resume` 根据策略继续；如果有已完成 ToolExecution，按稳定 invocation 复用结果；Plan/Multi-Agent 父 Run读取 Child Checkpoint 重新计算就绪或汇合。当前恢复由本地 API/调用链触发，ActiveRunSupervisor 的内存 Task 不会跨进程保存，也没有自动多机抢占。

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

**先给结论：** Tool retry 不会。一次逻辑调用沿用稳定 `invocation_id`；`ToolExecutionRecord` 持久化 attempt、最后失败类别、累计 backoff、`next_retry_at` 和 exhausted/suppressed 状态。

**60～120 秒完整口语答案：** 每次真实 Tool attempt 之前先在预算账本消费 Tool call，再把递增后的 attempt 与 RUNNING 状态落库。暂态失败后，Runtime 持久化失败类别和下一次允许重试时间，再进入可取消 backoff。若进程在等待期间退出，恢复读取同一 ToolExecution，等待尚未结束的剩余时间，然后从下一个 attempt 继续；若额度已用完则复用结构化失败，不会偷偷再执行。已经 `SUCCEEDED` 的结果仍直接复用且不重复收费。LLM 请求也会逐次计入 model-call 与可观测 Span，但供应商未报告的崩溃窗口 usage 无法凭空恢复，这是当前限制。

**第一轮追问：** “父 Run 只剩两秒怎么办？”——先算 root/local 剩余 wall time；若 backoff 或下一 attempt 放不下，就不再 retry，外层 deadline 优先。

**第二轮追问：** “取消时正在 sleep？”——backoff 使用可取消的 async wait，`CancelledError` 不被吞掉，也不会伪装成 `DEPENDENCY_RETRY_EXHAUSTED`。

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

#### P0-10｜“Map-Reduce Summary 怎么做？为什么不是直接总结全部历史？”

**面试官为什么问：** 简历点名 Map-Reduce，面试官会问分段、合并、成本和错误。

**先给结论：** Map 阶段分别压缩可管理的历史片段，Reduce 阶段合并为统一摘要；它解决单次输入过长和分段处理问题，但摘要本身可能丢失或幻觉，因此不能成为唯一恢复事实。

**60～120 秒完整口语答案：** 长会话先按边界分段，每段提取目标、约束、结论、未完成事项和证据，再合并去重成更短 summary。相比一次把全部历史交给模型，它更适合超过单次窗口的输入，也可复用已有阶段摘要。缺点是多次总结会积累误差，而且压缩本身消耗 Token。Axiom 将 durable state 与 summary 分开；摘要失败不应破坏原 Checkpoint，必要时保留近期原文或返回明确 ContextBudget 错误。

**第一轮追问：** “摘要 hallucinate 怎么办？”——固定结构字段、保留关键原文引用、确定性规则优先，并通过 retention/任务评测检测；不能把模型摘要当唯一事实。

**第二轮追问：** “什么时候触发？”——按估算 Token 的高水位而非轮数；压缩到目标区间，硬上限仍是最终门禁。

**常见坑：** “Map-Reduce 能无损压缩”；忽略摘要调用的成本和失败。

**项目证据：** `src/axiom/memory/summarizer.py`、`src/axiom/context.py`、context tests。

#### P0-11｜“什么内容不能随便压缩？Tool Result 有十万 Token 怎么办？”

**面试官为什么问：** 检查 Context 策略是否只追压缩率，而忽略协议和任务语义。

**先给结论：** 系统协议、当前目标/约束、尚未完成任务、关键证据和 Tool Call/Result 配对不能被静默破坏。超大 Tool Result 应保存原始 durable evidence，只向模型投影名称、状态、关键片段和可追溯引用。

**60～120 秒完整口语答案：** Tool Call 与 Result 是一个协议单元，只删一半会让模型消息非法或误解调用是否发生。Axiom 先按 message unit 分组，保留 recent raw 和结构化 summary；历史大 Tool 输出使用有界 projection，并明确标记裁剪。若 pinned 内容本身已超过 hard limit，就在 provider 调用前失败，而不是悄悄删除目标。Token 估算可能不准，所以要预留安全余量，并在 provider 返回真实 usage 后核销 RunBudget。

**第一轮追问：** “pinned context 是什么？”——在当前任务中不能随普通历史淘汰的协议、目标、约束和开放工作；并非所有 system 文本都可无限 pin。

**常见坑：** 直接截字符串；只保留最后 N 轮；把原始 Tool evidence 从持久层删除。

**项目证据：** `context.py:_message_units()`、`_tool_projection()`、hard-limit tests。

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

**30～60 秒回答：** Checkpoint CAS 会让一个状态写成功、另一个冲突，但二者可能已经同时产生外部调用，所以 CAS 不足以阻止双执行。当前用进程内锁和 Supervisor 限制本地并发；多进程要 lease/atomic claim/fencing。

**典型继续追问：** “fencing 是什么？” **回答思路：** 递增所有权令牌，存储/下游拒绝旧令牌写。**项目边界：** 当前没有多进程 Run ownership。

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

#### P2-4｜“为什么不用进程内存快照，恢复不是更完整吗？”

**最安全的结论：** 进程快照更耦合运行时和资源对象，业务 Checkpoint 更稳定、可演进和可审计。

**回答主线：** 连接、锁、Task 和库内部状态难以跨版本恢复；显式模型只保存业务最小事实，代价是要认真设计状态机和迁移。

**不要说什么：** “内存快照技术上不可能”；它只是当前场景取舍不优。

**如果继续扩展怎么做：** 版本化 schema、迁移工具和恢复兼容测试。

## 10. 重点追问树（7 条）

### 追问树 1：状态模型

简历列 Thread/Turn/Event/Run/Checkpoint → 追问为什么拆 → 回答交互历史 vs 执行恢复 → 继续追 Event 与 Checkpoint 一致性 → 回答恢复以 Checkpoint 为事实，事件负责重放，生产用事务/outbox 缩小窗口。

### 追问树 2：保存边界

你说 LLM/Tool 边界 → 追问为何不每 token 保存 → 回答写放大与半成品语义 → 继续追 LLM 完成但保存失败 → 回答可能重做模型、增加成本但无外部写 → 再追 Tool 完成 → 进入幂等不确定窗口。

### 追问树 3：CAS

你说版本号 → 追问 CAS → 回答预期版本防 stale write → 继续追与事务/锁区别 → 回答原子、互斥、乐观前置条件 → 再追两个 Worker双执行 → 回答 CAS 不够，需要 lease/fencing。

### 追问树 4：Tool 恢复

简历写 stable invocation → 追问怎么稳定 → 回答 Run/Tool call 派生并校验参数哈希 → 继续追成功未落库 → 回答 unknown → 再追 exactly-once → 回答服务端幂等/状态查询/补偿。

### 追问树 5：Map-Reduce Context

简历写 Map-Reduce → 追问为何两阶段 → 回答分段压缩再合并 → 继续追 hallucination → 回答结构字段、原文引用和 retention eval → 再追摘要失败 → 回答原 Checkpoint 不变、规则投影或明确失败。

### 追问树 6：预算与进展

你说 Token Budget → 追问 Context vs Run Budget → 回答单次窗口 vs 累计资源 → 继续追 Child 超卖 → 回答 root ledger 预留/核销 → 再追死循环 → 回答 max_steps 加 ProgressDetector。

### 追问树 7：压缩质量攻击

你说“保护结构” → 追问是否证明不变笨 → 回答不能 → 继续追 retention golden set → 回答目标、约束、open task、artifact、证据 → 再追 Token 降而成功也降 → 回答以业务成功和 cost per success 决策，不能只看压缩率。

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

SQLite/Memory Runtime store、Checkpoint CAS、LLM/Tool/interrupt 边界、stable invocation、ToolExecution 成功复用和 durable retry state、分层 timeout/deadline、安全 retry 与 full jitter、ReAct 进程重启恢复、上下文高水位/目标/硬上限、Tool 投影、Map-Reduce 会话摘要、10-case Context Retention 离线评测、根 Run 预算账本、无进展检测。

### 【当前部分支持】

外部写副作用只能在有幂等契约时安全重试；SQLite 适合单 Runtime；Context retention 能验证声明事实但没有证明开放域语义等价；模型价格可能未知；本地 supervisor 不能做多进程接管。

### 【未来可扩展】

PostgreSQL 共享状态、Run lease/heartbeat/fencing、幂等 Tool contract、语义级 Context 质量评测、按租户全局预算、自动 Worker 接管和跨实例事件总线。

## 13. 面试前 5 分钟速背

### 5 个概念

Checkpoint；CAS；stable invocation；Context projection；root Run budget/no-progress。

### 5 个源码锚点

`runtime/durable.py`；`runtime/checkpoints.py`；`context.py`；`runtime/budget.py`；`runtime/progress.py`。

### 必须不思考就能回答的 5 道 P0

怎样恢复？Tool 半成功怎么办？三种状态数据有何区别？怎样证明压缩质量？如何阻止死循环？

### 3 个陷阱

不承诺外部 exactly-once；不把 CAS 当租约；不拿压缩比冒充任务质量。

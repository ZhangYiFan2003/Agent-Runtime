# 简历五：Runtime API、可观测性与质量闭环

> 本册回答“怎样控制正在运行的任务、怎样定位问题、怎样把坏例变成回归门禁”。

## 0. 简历原句

> 构建 Runtime API，支持 Run 创建/查询、interrupt / resume / cancel 与 SSE 事件重放；基于 Trace / Span 记录延迟、TTFT、Token 与 Tool 调用指标，并支持固定任务集回归评测。

## 1. 这条经历解决了什么问题

一个能调用 LLM 和 Tool 的循环还不是可运营 Runtime。长任务需要在另一个请求里查询、暂停、恢复或取消；用户断线后需要从持久事件继续看进度；线上出错要知道慢在模型、工具还是数据库；模型或 Prompt 变更后，还要判断“错误率没升但任务质量是否下降”。

Axiom 把控制面、进程内 Active Run、事件重放、调用链追踪和离线评测连成一条闭环。它的重点是本地/企业内部可解释运行，不是公共 SaaS 网关；SSE 当前以持久事件重放为主，不应夸大成无限实时分布式流。

## 2. 30～45 秒主回答

Runtime API 把一次 Agent 执行暴露成可查询的 durable Run，支持创建、查看父子状态，以及幂等的 interrupt、resume、cancel。`ActiveRunSupervisor` 维护本进程里的 asyncio Task，使 cancel 能线程安全地通知活跃任务；真正结果仍写回 Checkpoint。事件用单调 ID 持久化，通过 SSE `after_id` 重放。

可观测性上，每个 Run 有稳定 Trace，LLM、Tool、计划和 Worker 阶段形成 Span，记录总延迟、首字时间 TTFT、Token、重试和成本。评测通过 Durable Runtime 跑固定任务，多次 trial 后计算 `trial_success_rate`、步骤、Token、延迟和每次成功成本；失败可收集为 Badcase，经人工审核后晋升回归集。当前结果 schema 是 v3，不是旧文档中的 v2。

## 3. 2～3 分钟完整故事

控制面首先要区分持久状态与活跃执行对象。Checkpoint 告诉系统 Run 处于什么业务状态；`ActiveRunSupervisor` 只维护本进程的 Task、事件循环和取消信号。cancel 到来时，Supervisor 用 `call_soon_threadsafe` 通知对应 Task，同时 Runtime 将取消意图和终态持久化。进程重启后内存 registry 消失，但 Checkpoint 仍在；这也是为什么它不能被宣传成多机所有权服务。

Runtime API 还处理控制操作幂等和事件历史。ThreadEventRepository 为事件分配单调 ID，客户端断线后带 `after_id` 读取后续事件，并可按 Run 过滤。这比只推内存消息可靠，但当前实现主要是 stored replay，不是 Kafka 式跨实例实时总线。Thread/Turn/Event 与 Run/Checkpoint 分离，前者服务交互历史，后者服务执行恢复。

调用链追踪（Tracing）把一次 Run 拆成可解释阶段。TTFT 是发起模型请求到收到首个文本/思考/Tool delta 的时间，和完整生成延迟不同；Tool Span 使用稳定 invocation 衔接恢复；RunMetrics 汇总 Token、步骤、工具、checkpoint、budget 和成本。当前 Trace 保存在 Memory/SQLite store，适合本地诊断，并未接入 OpenTelemetry 分布式链路。

质量闭环不能只看 HTTP 500。Evaluation 使用真实 Durable Runtime，而不是直接 `llm.chat()`，每个 trial 都有独立 Run/Thread/Turn/Trace。确定性 scorer 检查完成状态、包含内容、工具使用和指标阈值。失败收集为带有限证据和指纹的 Badcase，状态从 PENDING 经人工 APPROVED 后才能 PROMOTED 到回归数据集；baseline/candidate 比较既看功能退化，也看 trial success、Token、延迟、步骤和 cost per success。

最后必须讲完成证明的边界：模型说“完成”只代表终止提议。现在可选 Completion Contract 会在终止点检查 Run status、ToolExecution、输出、workspace artifact、Plan task 与 Child Run；通过才进入 COMPLETED。ReAct 第一次失败会收到一条结构化反馈并按普通 Budget 再走一步，第二次仍失败则以 `COMPLETION_NOT_VERIFIED` 结束。没有契约的开放任务保持兼容并标记 NOT_APPLICABLE，而不是伪造客观验证。

Runtime completion verification 与 offline Evaluation 不是一件事。前者在一次配置了契约的
Run 的终止边界决定能否进入 `COMPLETED`；后者在独立 trial 中比较版本、任务成功率、资源
和坏例。一个验证结果会成为 Trace/metrics 和 Evaluation 的输入证据，但不会替代 repeated
trials、Badcase 审核或 Regression gate。

## 4. 架构与调用链

```text
Client / IDE
  ↓ Runtime API
Run create/query/control ──→ ActiveRunSupervisor（本进程 Task）
  │                               │ cancel signal
  ↓                               ↓
DurableAgentRuntime → Checkpoint / ToolExecution
  │
  ├─ ThreadEventRepository → SSE stored replay(after_id)
  └─ Agent proposes final → Completion Verification
                               ↓
                         Trace / RunMetrics
                               ↓
固定 Dataset → repeated trials → Scorers → Badcase review → Regression dataset → Regression gate
```

## 5. 最新源码实现

- `src/axiom/runtime/api.py`：`RuntimeApiServer`、ThreadEventRepository、Run/parent/assignment view、控制操作和 SSE 重放。
- `src/axiom/runtime/control_plane.py`：控制操作记录与幂等语义。
- `src/axiom/runtime/supervisor.py`：`ActiveRunSupervisor`、`ExecutionHandle`、取消与 shutdown/drain。
- `src/axiom/runtime/observability.py`：Trace、Span、RunMetrics 与稳定 ID。
- `src/axiom/runtime/observability_store.py`：Memory/SQLite store、`RunTracer`、聚合服务。
- `src/axiom/evaluation/runner.py`：`DurableEvaluationExecutor` 与 `EvaluationRunner`。
- `src/axiom/evaluation/models.py`：dataset schema v1、result schema v3、trial aggregate 与 cost per success。
- `src/axiom/evaluation/scorers.py`、`badcases.py`、`comparison.py`、`attribution.py`：确定性评分、坏例审核、回归门禁和安全指纹。
- `tests/test_runtime_api_hardening.py`、`test_active_run_supervisor.py`、`test_observability.py`、`test_evaluation.py`、`test_evaluation_feedback.py`：执行证据。

## 6. 必须讲清的技术点

**TTFT（Time To First Token，首字时间）** 衡量用户多久看到首个模型增量；总延迟衡量请求何时完全结束。流式返回可以改善感知延迟，但未必降低总计算时间。

**Trace** 表示一次 Run 的整体链路，**Span** 表示其中一个阶段。指标告诉你“哪里异常”，调用链告诉你“哪一步慢”，日志解释“为什么慢”。三者互补。

**SSE 重放** 是客户端带游标读取已存事件；它解决断线续看。当前并非跨实例低延迟事件总线，也不保证永远保持连接实时推送。

**trial_success_rate** 是重复试验的直接成功比例，不是形式化 Pass@k。**cost per success** 比单次平均成本更接近 Agent 价值：便宜但经常失败的候选未必更好。

**Attribution fingerprint** 记录模型、Prompt、Tool schema、策略和数据集的稳定指纹，帮助归因版本差异；它提供关联证据，不自动证明因果。

## 7. 设计理由、替代方案与取舍

为什么 Supervisor 与 Checkpoint 分开？内存 Task 适合即时取消，数据库状态适合恢复和查询；把两者混为一谈会在重启后失去事实来源。为什么持久事件而不是仅内存 SSE？断线后可补看，但付出存储与清理成本。

为什么评测必须通过 Runtime？直接调用模型会绕过 Tool、Context、Budget、Progress 和恢复逻辑，无法发现真正集成回归。为什么 deterministic scorer 优先？它可复现、可解释、便于 CI；开放式质量仍可未来引入 LLM judge 和人工抽样，但要处理 judge 漂移。

SQLite Trace 和 JSON/Badcase store 适合本地。共享服务再引入 OpenTelemetry、集中指标/日志、共享数据库和跨实例事件系统。先把语义和证据做好，比先安装一套可观测平台更重要。

## 8. 故障与边界场景

- 重复 resume/cancel：控制操作有幂等身份并校验状态，不能重复推进副作用。
- API 线程取消异步 Task：通过 Supervisor 的事件循环句柄线程安全通知。
- 进程突然退出：活跃 registry 消失；Checkpoint 可恢复，但不会自动被另一机器接管。
- SSE 客户端断线：使用最后事件 ID 重放；需要保留策略避免事件无限增长。
- Trace Span 未正常结束：保留错误/状态并在汇总时区分缺失数据，不能伪造 0ms。
- 模型升级后 HTTP 错误率正常但成功率下降：固定任务、多 trial、Badcase 和回归比较负责发现。
- Badcase 含敏感 Tool 参数：只保存有界派生证据和引用，不复制完整 Trace/原始参数。

## 9. 分级面试题库（22 题）

### P0 — 简历直击题（10 题）

#### P0-1｜“一个 Agent 为什么还要单独做 Runtime API？普通 HTTP 接口不够吗？”

**面试官为什么问：** 简历开头就是 Runtime API。面试官要确认它解决了长任务控制问题，而不是为了显得像后端服务。

**先给结论：** 普通请求通常在一次连接内完成；Agent Run 可能持续几十秒、等待审批、产生 Child Run，还需要在另一个请求里查询、暂停、恢复和取消，所以必须有独立、持久的执行身份。

**60～120 秒完整口语答案：** 客户端发起一个 Turn 后，Runtime 创建可持久化 Run；Run 有状态、Checkpoint、Trace 和父子关系，即使原 HTTP 连接断开仍可查询。Runtime API 负责创建/查看 Run、控制 interrupt/resume/cancel，并通过事件重放让客户端续看。它把“网络请求生命周期”和“业务执行生命周期”分开。当前 API 面向本地/企业内部控制面，不包含公网网关、租户认证、分布式队列和自动扩缩容，这些不能从“有 HTTP API”推导出来。

**第一轮追问：** “Run 和 HTTP request 最大区别是什么？”——request 是一次传输交互，Run 是跨请求、可恢复、可控制的业务执行实体。

**第二轮追问：** “为什么创建 Turn 后还要 Run？”——Turn 表示用户交互；一个 Turn 可以包含父 Run 和多个 Child Run，控制与资源必须落到 Run。

**常见坑：** “CLI 加个 HTTP 包装就是 Runtime API”；把 API 存在等同于生产 SaaS。

**项目证据：** `src/axiom/runtime/api.py`、`src/axiom/runtime/models.py`、API hardening tests。

#### P0-2｜“interrupt、cancel、resume 到底有什么区别？”

**面试官为什么问：** 三个控制词直接写在简历上，必须能说清语义而非都是 `task.cancel()`。

**先给结论：** interrupt 把 Run 停在可恢复状态，resume 从该状态继续；cancel 表示终止意图，目标是收敛到不可继续的 CANCELLED。三者都必须改变 durable state，不能只操作内存 Task。

**60～120 秒完整口语答案：** interrupt 适合用户暂停、等待外部信息或审批，它保存原因和当前 Checkpoint；resume 校验当前状态和可选决策后继续推进；cancel 则既要通知本进程活跃 Task，又要持久化取消事实，避免进程重启后任务复活。正在执行的 Tool 可能不响应取消，或者外部副作用已经发生，所以 Runtime 只能尽快停止后续步骤，并按 ToolExecution 状态处理未知窗口，不能承诺把外部世界回滚。

**第一轮追问：**

- **追问：** “cancel 正在执行的 Tool，会立即停吗？”
  - **回答思路：** asyncio 是协作取消，外部调用和副作用有边界。
  - **30～60 秒可直接说出的答案：** “不保证立即停。Task 通常在下一个可取消的 `await` 点收到取消；同步阻塞 Tool 或已经提交到远端的操作可能继续。系统要设置 Tool deadline、停止后续步骤并记录最终状态，写副作用再按幂等或补偿处理。”

**第二轮追问：** “interrupt 和 WAITING_APPROVAL 一样吗？”——都是可恢复等待，但原因和允许的 resume 决策不同，应由状态机校验。

**常见坑：** 三者都解释为杀协程；声称 cancel 能撤销已发生副作用。

**项目证据：** `DurableAgentRuntime.interrupt/resume/cancel()`、control plane tests。

#### P0-3｜“同一个 cancel 或 resume 调两次怎么办？为什么控制操作要幂等？”

**面试官为什么问：** 控制 API 会因网络重试、用户双击和并发客户端重复到达。

**先给结论：** 相同逻辑操作重复提交应返回同一结果或当前状态，不应再次推进 Run。Axiom 通过控制操作身份、状态校验和 Checkpoint 版本共同防止重复副作用。

**60～120 秒完整口语答案：** 客户端可能第一次 cancel 已成功但响应丢失，于是重试。若服务端把第二次当新命令，可能重复写事件或与 resume 竞争。Runtime API 会规范化控制请求并记录/重放幂等操作结果，同时状态机只允许合法转换；底层 Checkpoint CAS 再防过期写。幂等不等于并发完全消失：两个不同控制意图仍可能竞争，需要明确谁成功、谁收到冲突或当前状态。

**第一轮追问：** “只用数据库唯一键够吗？”——它能去重同 key，但还要绑定 operation、Run、参数和结果，并校验状态转换。

**常见坑：** “GET 才需要幂等”；或把 CAS 与 API 幂等当同一个机制。

**项目证据：** `src/axiom/runtime/control_plane.py`、`RuntimeApiServer._execute_control_operation()`。

#### P0-4｜“Run 有哪些主要状态？Child Run 怎么和 Parent 对上？”

**面试官为什么问：** 简历写创建/查询 Run，面试官会问查询返回什么以及多 Agent 怎样表达。

**先给结论：** 当前主要状态包括 RUNNING、INTERRUPTED、WAITING_APPROVAL、WAITING_CHILD、COMPLETED、FAILED、CANCELLED；Child 通过 parent/assignment lineage 与父 Run 关联。

**60～120 秒完整口语答案：** 状态要区分可继续等待和终态：等待审批与等待 Child 不能被误报为失败，cancel 与 failed 的原因也不同。Plan/Multi-Agent 父 Run在启动 Child 后进入等待，查询视图可以展示 parent_run_id、assignment/task 元数据和子状态。父 Run恢复时不是相信内存列表，而是读取 Child Checkpoint 做 reconciliation。当前 lineage 在本地存储/API 中可查，但不是跨集群工作流可视化平台。

**第一轮追问：** “为什么不能只有 running/success/fail？”——无法表达暂停、审批和父子等待，也无法安全决定 resume 是否允许。

**常见坑：** 把 WAITING 当失败；认为 Child 只是日志字段。

**项目证据：** `RunStatus`、`RuntimeApiServer._children()`、`_run_view()`。

#### P0-5｜“为什么选择 SSE？和 WebSocket 怎么选？”

**面试官为什么问：** 简历明确写 SSE，面试官会自然追 HTTP 长连接和双向通信取舍。

**先给结论：** Agent 输出主要是服务端向客户端单向增量事件，SSE 基于普通 HTTP、文本事件和自动重连语义，足够简单；需要高频双向实时消息或二进制帧时才更适合 WebSocket。

**60～120 秒完整口语答案：** SSE 用一个 HTTP 长连接持续发送带 `id/event/data` 的文本事件，代理和浏览器支持较自然。Axiom 的控制命令仍通过普通 HTTP 请求发送，输出/事件走 SSE，因此无需为低频控制建立全双工通道。WebSocket 在双向协作、终端输入或高频互动时更灵活，但连接状态、心跳、扩容和重连恢复要自己设计。协议选择不自动解决可靠性，真正断线续看仍依赖事件持久化和游标。

**第一轮追问：** “SSE 能发二进制吗？”——通常传文本，二进制需编码或改用其他通道；Agent token/event 多为 JSON 文本。

**常见坑：** “SSE 一定比 WebSocket 性能好”；说 SSE 天然保证不丢事件。

**项目证据：** `src/axiom/runtime/api.py` 的 event envelope 与 `_send_events()`。

#### P0-6｜“客户端断线后怎么续？`after_id` 能避免漏和重复吗？”

**面试官为什么问：** “事件重放”比“流式输出”更强，面试官会追游标和投递语义。

**先给结论：** 每个持久事件分配单调 ID；客户端记住最后已处理 ID，重连时请求 `after_id` 之后的事件。它能补历史，但客户端仍应按事件 ID 幂等处理重复。

**60～120 秒完整口语答案：** 关键顺序是先把事件持久化并获得 ID，再让客户端读取。连接在发送后、客户端确认前断开时，重连可能再次收到边界事件，所以展示侧按 ID 去重更稳；若事件尚未持久化就只在内存推送，则无法可靠补回。Axiom 当前是 stored replay，可按 Run filter 查询；它不应被宣传成无限保持的实时总线。事件保留、分页和多实例一致性仍是未来生产问题。

**第一轮追问：** “stored replay 和 live stream 什么区别？”——前者读已存事实，可靠续传更强但有查询延迟；后者强调低延迟推送，通常还需 broker/订阅机制。

**第二轮追问：** “多实例时怎么办？”——事件写共享存储或 durable broker，SSE 节点无状态消费；当前 SQLite 本地实现不支持这一层。

**常见坑：** 说 `after_id` 带来 exactly-once UI；忽略客户端去重和保留期限。

**项目证据：** `ThreadEventRepository.append_event/list_events()`、SSE API tests。

#### P0-7｜“Trace、Span、Metric、Log 有什么区别？为什么日志不够？”

**面试官为什么问：** 简历写 Trace/Span 和指标，这是可观测性最基本的概念链。

**先给结论：** Trace 串起一次 Run 的端到端路径，Span 表示其中一个阶段；Metric 聚合趋势和分位数；Log 保存离散诊断细节。指标先发现异常，Trace 定位慢在哪一步，日志解释为什么。

**60～120 秒完整口语答案：** 如果 P99 上升，Metric 能告诉我影响范围和时间；选一条慢 Run 后，Trace 展开 LLM、Tool、Checkpoint、Plan/Worker Span，找到 4 秒到底花在哪；再用该 Span 的错误和日志检查连接池、429 或参数。只有日志时，很难跨异步 Child Run 还原顺序，也难计算 P95/P99；只有 Trace 又看不到全局趋势。Axiom 用稳定 run-derived trace identity 对齐状态和观测，但当前存储是本地 Memory/SQLite，并非完整分布式 APM。

**第一轮追问：** “Run 和 Trace 怎么对上？”——Trace ID 由稳定 Run ID 派生，Tool Span 也能用稳定 invocation 对齐恢复前后调用。

**常见坑：** 四个词互相替代；说加日志就能计算可靠尾延迟。

**项目证据：** `src/axiom/runtime/observability.py`、`observability_store.py`。

#### P0-8｜“TTFT 是什么？流式返回真的降低延迟了吗？”

**面试官为什么问：** TTFT 是简历显式指标，也最容易把感知延迟和总耗时混淆。

**先给结论：** 首字时间（TTFT）是发起模型请求到收到第一个有效增量的时间；流式返回可以改善用户感知，但不一定降低完整生成时间、成本或资源占用。

**60～120 秒完整口语答案：** 如果模型 900ms 后出首字、5 秒生成完成，TTFT 是约 900ms，总延迟是约 5 秒。流式让用户早点看到进度，但 Worker 和 provider 仍被占用到最后。TTFT 变慢通常看入口排队、Prompt/输入 Token、连接建立和 provider 调度；首字正常但总延迟慢则看输出长度、生成速率和 Tool 后续。Axiom 在 LLM Span 中分开记录这些信息，避免用“开始流式”冒充端到端优化。

**第一轮追问：** “第一个什么算首字？”——应预先定义首个文本/思考/Tool delta 的观测口径并保持一致，不能版本间随意变。

**常见坑：** “Streaming 把 5 秒请求降到 1 秒”；只看平均延迟。

**项目证据：** Durable Runtime LLM stream collection、`RunMetrics`、observability tests。

#### P0-9｜“Token 和 Tool 调用指标具体能帮你看什么？P99 高怎么判断慢在谁？”

**面试官为什么问：** 简历不只是说记录日志，而是点名 Token、Tool 与 latency。

**先给结论：** Token 解释模型输入/输出规模和成本，Tool 指标解释调用次数、失败、重试与延迟；把它们放进同一 Trace，才能区分 LLM 慢、Tool 慢、Checkpoint 慢或步骤变多。

**60～120 秒完整口语答案：** 我会先看 QPS、错误率、P50/P95/P99 和资源饱和，再抽取 P99 慢样本。若 LLM Span TTFT 高且输入 Token 增大，优先查 Context；若 Tool Span 集中在某 MCP，查连接池和依赖；若每步都正常但总时长变长，看平均步骤和重复调用；若 checkpoint Span 等待上升，看 SQLite 写竞争。临时止血是降低并发、暂停问题依赖或回滚，完整生产排障与容量治理放在第 8 册。

**第一轮追问：** “只看平均值为什么不够？”——少量超慢请求会被平均稀释，P99 更能暴露排队、锁竞争和长尾依赖。

**常见坑：** 一上来猜数据库；把 Token 多直接等同于答案更好。

**项目证据：** `RunMetrics`、LLM/Tool/checkpoint spans。

#### P0-10｜“固定任务回归评测怎么做？为什么不能只比较最终文本？”

**面试官为什么问：** 简历最后一项是 Evaluation，面试官会追数据、Ground Truth 和执行链。

**先给结论：** 固定任务集要版本化 prompt、expected/scorers 和运行配置，并通过真实 Durable Runtime 执行。Agent 有多条正确路径，不能只做最终字符串相等，还要检查状态、必要内容、Tool 使用、步骤、Token 和延迟。

**60～120 秒完整口语答案：** 每个 EvaluationCase 定义任务和确定性 scorer，例如 Run 必须 COMPLETED、答案包含关键文件、必须使用 code search、禁止 write_file，或者步骤不超过阈值。每个 trial 使用独立 Run/Thread/Turn/Trace，避免状态污染。baseline 与 candidate 在相同 dataset 和配置下比较，既看功能成功，也看资源变化。Ground Truth 需要人工审核和版本化；开放式表达可用 contains/结构证据，不能为了方便把模型输出逐字固定。

**第一轮追问：**

- **追问：** “deterministic scorer 有什么盲区？”
  - **回答思路：** 可复现但覆盖有限，容易漏掉语义正确性和表达质量。
  - **30～60 秒可直接说出的答案：** “它很适合检查状态、必需事实、Tool 约束和资源阈值，结果稳定可解释；但复杂答案可能满足关键词仍逻辑错误，也可能正确改写却没命中字符串。所以要组合多个 scorer，对开放任务再加人工抽样或校准过的 judge。”

**第二轮追问：** “为什么评测要走 Runtime？”——直接 `llm.chat()` 会绕过 Tool、Context、Budget、Progress、Checkpoint 和 Trace，测不到集成回归。

**常见坑：** 只做 exact match；让评测绕过真实执行链。

**项目证据：** `src/axiom/evaluation/runner.py`、`scorers.py`、`benchmarks/datasets/agent-core.json`。

### P1 — 回答后的自然深挖（8 题）

#### P1-1｜“数据库已经写 CANCELLED，为什么还要 Supervisor？”

**为什么会被追到这里：** 你解释 cancel 有持久状态和即时信号两部分。

**30～60 秒回答：** 数据库是事实源，但正在 `await` 的本地 Task 不会自动轮询到状态变化；Supervisor 保存 Run 到 Task/Event Loop 的映射，立即发取消信号。只做内存取消又无法跨重启保留意图，所以两层都要。

**典型继续追问：** “进程重启后 Supervisor 呢？” **回答思路：** 内存 registry 消失，以 Checkpoint 恢复。**项目边界：** Supervisor 是 process-local。

#### P1-2｜“Task 在另一个线程的 Event Loop，怎么安全取消？”

**为什么会被追到这里：** Runtime API 的请求线程与执行 loop 可能不同。

**30～60 秒回答：** 不能直接跨线程随意操作 Task；保存其 loop 句柄，用 `call_soon_threadsafe` 把取消回调投递到所属事件循环，由 loop 线程执行 `task.cancel()`。

**典型继续追问：** “cancel 后为何还要收敛状态？” **回答思路：** `CancelledError` 是执行信号，Checkpoint 的 CANCELLED 才是业务事实。**项目边界：** 同步阻塞代码仍可能延迟响应。

#### P1-3｜“shutdown 时正在运行的任务怎么办？”

**为什么会被追到这里：** 你提到了 ActiveRunSupervisor。

**30～60 秒回答：** 先停止接受新任务，再请求活跃 Run 取消并给有限 drain 时间；超时后结束进程，但尽量持久化取消/中断意图。外部 Tool 副作用仍按 ToolExecution 状态恢复。

**典型继续追问：** “能无限等吗？” **回答思路：** 不能，服务关闭要有 deadline。**项目边界：** 当前是本地 shutdown，不是编排平台的优雅下线协议。

#### P1-4｜“为什么要 repeated trials？`trial_success_rate` 是 Pass@k 吗？”

**为什么会被追到这里：** 你说模型结果有随机性。

**30～60 秒回答：** 同一任务一次成功或失败方差很大，多次独立 trial 才能看到稳定性。Axiom 的 `trial_success_rate` 是成功次数除以总试验数，不是形式化 Pass@k 估计，不能换名字包装。

**典型继续追问：** “一般跑几次？” **回答思路：** 取决于方差、成本和可接受置信度，没有万能数字；CI 可少量，发布前重点集更多。**项目边界：** 当前默认行为和具体运行次数以命令配置为准。

#### P1-5｜“baseline 和 candidate 怎么公平比较？”

**为什么会被追到这里：** 你介绍了回归对比。

**30～60 秒回答：** 固定 dataset、scorer、工具与策略配置，记录模型/Prompt/Tool schema/Context 指纹；每个候选独立多 trial，尽量控制 provider 时段和限流干扰。仍要报告方差，不能把随机差当确定回归。

**典型继续追问：** “模型服务同时升级了呢？” **回答思路：** attribution 可发现变量变化，但需控制变量复跑；固定公开开发集、隐藏或冻结 holdout，避免根据最终集结果反复改 Prompt，造成测试集泄漏。**项目边界：** 指纹提供关联，不证明因果。

#### P1-6｜“Badcase 是什么？为什么不自动加进回归集？”

**为什么会被追到这里：** 你从失败复盘谈到质量闭环。

**30～60 秒回答：** Badcase 是一次失败及其有限、可审核证据。Provider outage、错误 Ground Truth 或偶发环境问题不一定是产品缺陷，所以先 PENDING，经人 APPROVED 并补足确定性期望后才 PROMOTED。

**典型继续追问：** “怎么避免重复收集？” **回答思路：** 稳定身份和幂等写入。**项目边界：** Badcase store 当前本地，不是企业标注平台。

#### P1-7｜“Regression Gate 拦什么？Token 降了但成功率也降了怎么算？”

**为什么会被追到这里：** 你说评测不只看成功。

**30～60 秒回答：** Gate 优先拦功能成功退化，也可限制 trial success drop 和 cost per success 增长。Token 降低不是单独目标；若成功率下降导致每次成功成本更高，通常不是优化。阈值应基于任务风险和 baseline，不设万能值。

**典型继续追问：** “为什么 cost per success 更合理？” **回答思路：** 总成本除以成功次数，同时惩罚失败重试。**项目边界：** 价格未知时不能伪造 0 成本。

#### P1-8｜“线上 Trace 要不要全采？怎么避免泄露 Tool 参数？”

**为什么会被追到这里：** 你把本地观测自然扩展到生产。

**30～60 秒回答：** 高价值错误和慢请求可提高采样，正常流量按比例；属性使用 allowlist、长度上限和脱敏，不记录 API Key、原始敏感参数或大 payload。Trace ID 保留关联，内容按租户访问控制。

**典型继续追问：** “尾部采样怎么做？” **回答思路：** 完成后按错误/延迟决定保留，需要 collector 缓冲。**项目边界：** 当前没有分布式采样平台。

### P2 — 攻击、边界与架构取舍（4 题）

#### P2-1｜“模型说任务完成，你凭什么相信它真的完成？”

**最安全的结论：** 模型说“完成”只是一项终止提议。若任务提供 Completion Contract，Runtime 会用确定性证据验证；无契约的开放任务仍可能 COMPLETED，但其 verification status 是 NOT_APPLICABLE，不能称为客观验证。

**回答主线：** 当前流程是模型/strategy 提出完成 → Completion Contract → CompletionVerifier。Tool 是否成功读取持久 `ToolExecutionRecord`，Plan 是否结束读取 task/Child state，文件检查限定 workspace，命令/测试成功必须来自正常 Tool Runtime 的成功记录，不偷偷开 subprocess。结果显式区分 VERIFIED、NOT_VERIFIED、NOT_APPLICABLE、ERROR；Checkpoint 保存 contract、最新结果和 attempt，Trace/RunMetrics/Evaluation 记录状态、失败 check 与次数。ReAct 默认只允许一次反馈继续，额外模型调用和 Token 全部计入原 Run Budget。

**不要说什么：** “模型输出 final 就完成”“测试绿就覆盖全部业务”“LLM Judge 一定客观”。

**当前限制：** v1 不是 universal verifier，没有 LLM Judge，也不判断开放式语义正确性；测试通过只证明已编码契约。Plan/Multi-Agent 终止提议只验证一次，不引入新的无限规划循环。

#### P2-2｜“你的 SSE 能直接水平扩展吗？”

**最安全的结论：** 当前本地 SQLite stored replay 不能直接变成跨实例流。

**回答主线：** 多实例需要共享事件存储或 durable broker、无状态 SSE 节点、游标与保留策略；Run ownership 也要独立解决。

**不要说什么：** “负载均衡加 sticky session 就等于可靠重放。”

**如果继续扩展怎么做：** 共享 event log、broker fan-out、tenant filter 和断线游标。

#### P2-3｜“有 Trace/Span 就算生产可观测平台了吗？”

**最安全的结论：** 当前实现了语义模型和本地存储，不是分布式 APM。

**回答主线：** 还缺跨服务 context propagation、集中采集、采样、告警、SLO、租户脱敏和保留治理。

**不要说什么：** “SQLite Trace 可以替代 OpenTelemetry 平台。”

**如果继续扩展怎么做：** 保留稳定 Run/Span 语义，接 OTel exporter 和集中 metrics/logs。

#### P2-4｜“Attribution fingerprint 能证明是模型升级导致回归吗？”

**最安全的结论：** 不能证明因果，只能显示哪些输入版本发生变化并帮助控制实验。

**回答主线：** 如果模型、Prompt、Tool schema 同时变化，必须单变量或消融复跑；哈希只是可复现身份。

**不要说什么：** “只有哈希不同，所以根因确定。”

**如果继续扩展怎么做：** canary、随机分流、控制变量 A/B 和版本化报告。

## 10. 重点追问树（7 条）

### 追问树 1：Runtime API

简历写 Run 创建/查询 → 追问为什么不是普通 request → 回答执行跨连接持久存在 → 继续追 Thread/Turn/Run → 回答交互容器、用户回合和执行实例 → 再追 Child lineage → 回答 parent/assignment 与独立状态。

### 追问树 2：控制语义

简历写 interrupt/cancel/resume → 追问区别 → 回答暂停可恢复、终止和继续 → 继续追重复 cancel → 回答幂等 key、状态校验和 CAS → 再追 Tool 不停 → 回答协作取消与副作用窗口。

### 追问树 3：Supervisor

你说“通知活跃 Task” → 追问数据库 CANCELLED 为何不够 → 回答事实与即时信号分层 → 继续追跨线程 loop → 回答 `call_soon_threadsafe` → 再追重启后 registry → 回答消失，以 Checkpoint 恢复，非分布式 ownership。

### 追问树 4：SSE

简历写事件重放 → 追问为何 SSE → 回答单向增量、HTTP 简单 → 继续追断线 → 回答单调 ID/after_id → 再追重复/多实例 → 回答客户端幂等、共享 event log/broker。

### 追问树 5：可观测性

简历写 Trace/Span → 追问和日志区别 → 回答指标发现、Trace 定位、日志解释 → 继续追 TTFT vs 总延迟 → 回答感知与资源占用 → 再追 P99 → 回答按 Span 拆 LLM/Tool/checkpoint/queue。

### 追问树 6：Evaluation

简历写固定任务集 → 追问 Ground Truth/scorer → 回答版本化任务与多维确定性检查 → 继续追随机性 → 回答独立 repeated trials、不是 Pass@k → 再追 baseline 公平性 → 回答固定配置与 attribution，控制变量复跑。

### 追问树 7：完成证明

你说 Run COMPLETED → 追问“模型说完你就信？” → 回答终止提议不等于验证 → 继续追 Completion Contract → 回答持久 Tool/Plan/Child/artifact 证据 → 再追无确定性测试 → 回答 NOT_APPLICABLE、人工 rubric 与边界，明确没有 universal verifier 或 LLM Judge。

## 11. 面试官攻击面与防守口径

1. **“API 只是 CLI 套 HTTP？”** 用跨请求 Run、控制、恢复和重放说明。
2. **“cancel 就改数据库？”** 说明 Supervisor 信号与 durable state 两层。
3. **“SSE 天然可靠？”** 说明持久事件、after_id、客户端去重和保留。
4. **“Streaming 降低总延迟？”** 区分 TTFT、总时长、吞吐和成本。
5. **“有 Trace 就是生产 APM？”** 明确本地 store 与分布式平台差距。
6. **“一次 Eval 成功就稳定？”** repeated trials 与直接成功率。
7. **“Badcase 自动进集更快？”** 强调人审防脏数据和测试泄漏。
8. **“模型/测试说完成就完成？”** 使用 Completion Contract 与证据层级，明确当前边界。

## 12. 当前边界

### 【当前已实现】

Runtime API、Run/Child 查询和 lineage、interrupt/resume/cancel、控制操作幂等、本地 ActiveRunSupervisor、SSE stored replay、Trace/Span/RunMetrics、TTFT/Token/Tool/成本指标、Completion Contract/Verifier、固定数据集、多 trial、Badcase 人审晋升、baseline/candidate comparison、result schema v3 和 cost per success。

### 【当前部分支持】

Supervisor 仅拥有本进程 Task；SSE 主要重放持久事件；Trace/指标存在但不是集中生产平台；CompletionVerifier 只覆盖显式确定性契约，不等于通用业务完成验证；attribution 是相关证据。

### 【未来可扩展】

共享 Run lease、跨实例事件总线、OpenTelemetry、集中告警、租户级脱敏/采样、在线质量监控、语义 Judge、自动 canary 与回滚。

## 13. 面试前 5 分钟速背

### 5 个概念

控制面幂等；Supervisor 信号；SSE 重放；Trace/Span/TTFT；Badcase→Regression。

### 5 个源码锚点

`runtime/api.py`；`runtime/supervisor.py`；`runtime/observability.py`；`evaluation/runner.py`；`evaluation/badcases.py`。

### 必须不思考就能回答的 5 道 P0

三类 ID 怎么分？cancel 如何生效？SSE 怎样续传？怎样发现质量回归？模型完成为什么不可信？

### 3 个陷阱

不把内存 Supervisor 当分布式；不把 SSE replay 当 Kafka；不把模型 final 当验证结果。

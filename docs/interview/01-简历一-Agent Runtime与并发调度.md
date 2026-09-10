# 简历一：Agent Runtime 与并发调度

> 本册只讲执行策略、DAG、Child Run 与本地并发调度。持久化恢复见第 4 册，控制面与质量闭环见第 5 册。

## 0. 简历原句

> 实现 ReAct、Plan-and-Execute 与 Planner / Worker / Reviewer 多 Agent 模式；按 DAG 依赖拆分任务并由 asyncio 调度就绪节点，单批最多并发 4 个只读 Tool，写操作串行执行；在固定 Tool I/O 基准下较串行耗时降低约 74%。

## 1. 这条经历解决了什么问题

单一 ReAct 适合边观察边行动，但复杂任务会出现步骤不可见、独立工作无法并行、失败影响范围过大的问题。Axiom 没有强迫所有任务使用一种模式，而是把“怎样推进任务”抽成执行策略：简单任务走 ReAct，需要显式依赖的任务走 Plan-and-Execute，需要角色分工的任务走 Planner / Worker / Reviewer。

这里的核心不是“Agent 数量多”，而是把并发建立在依赖关系、读写语义和资源上限之上。只有依赖已经满足的节点才会进入就绪集合；独立 Child Run 可以并发；同一批 Tool 里只有同时声明只读且并发安全的调用才并发，写操作保持串行。

## 2. 30～45 秒主回答

我把 Axiom 的执行方式分成三层。第一层是 ReAct，模型每轮决定直接回答还是调用工具；第二层是 Plan-and-Execute，Planner 生成带依赖的任务图，调度器只启动依赖已完成的就绪任务；第三层是 Planner / Worker / Reviewer，Planner 拆分工作，Worker 以 durable Child Run 执行，Reviewer 检查后再汇总。

并发不是简单地 `gather` 所有任务。计划节点有依赖，Tool 还有读写副作用，所以系统分别限制 Child Run 并发和 Tool 并发；只有只读且声明并发安全的 Tool 才并行，写操作串行。固定基准只测 4 个各等待 200ms 的 ToolExecutor 调用，30 次运行中平均从约 828ms 降到约 208ms，改善 74.99%；这证明的是等待型 Tool 调度收益，不代表完整 Agent 端到端提速 74%。

## 3. 2～3 分钟完整故事

我先遇到的是执行模型选择问题。简单查询如果先规划，规划本身就是额外模型成本；复杂改代码任务如果只靠 ReAct，又容易重复探索。所以我保留了三个策略，并让它们复用同一个 DurableAgentRuntime。策略决定“下一步做什么”，Runtime 负责状态、预算、工具执行和观测，两者职责分离。

Plan-and-Execute 中，计划不是一个顺序列表，而是 DAG。一个任务只有在所有依赖都完成后才就绪。`LocalPlanTaskScheduler` 根据就绪集合和并发上限启动 Child Run；父 Run 保存计划状态并等待子任务，子任务完成后父 Run 再做汇合、必要时重规划，最后汇总答案。Multi-Agent 也采用类似思路：Planner 和 Reviewer 是串行父步骤，独立 Worker 是可并发 Child Run。

并发安全方面，我没有把“异步函数”误当成“可以并发执行”。`ToolExecutor.execute_all()` 只把 `is_read_only` 与 `is_concurrency_safe` 同时成立的调用放入并发批次，写文件、执行有副作用的命令等调用保持串行。`asyncio` 的价值是当模型、HTTP、MCP 或文件操作在等待 I/O 时，让事件循环推进其他就绪工作；它不会让 CPU 密集计算自动变快。

证据边界也要讲清。74% 来自 Windows、Python 3.12 环境下固定的合成 I/O 基准，范围只包含 ToolExecutor，不包含 LLM、进程启动和完整任务。因此我会用它证明调度实现有效，而不会包装成线上吞吐或业务成功率。

## 4. 架构与调用链

```text
请求
 ├─ ReActExecutionStrategy ── 模型 → Tool → 观察 → 下一轮
 ├─ PlanExecuteStrategy ──── Planner → DAG → 就绪 Child Runs → 汇合
 └─ MultiAgentExecutionStrategy
       Planner → Worker Child Runs → Reviewer → 汇总
                         │
                         ↓
                DurableAgentRuntime
                  │             │
            ToolExecutor   Checkpoint/Budget/Trace
                  │
        只读且并发安全：有界并发
        写操作：串行
```

## 5. 最新源码实现

### 入口与核心模型

- `src/axiom/agent/query_engine.py`：面向 CLI、SDK 的高层入口。
- `src/axiom/runtime/strategies.py`：`RuntimeExecutionStrategy`、`ReactExecutionStrategy` 与策略选择。
- `src/axiom/runtime/plan_strategy.py`：`PlanExecuteStrategy`、`LocalPlanTaskScheduler`、计划任务到 Child Run 的映射。
- `src/axiom/runtime/multi_agent_strategy.py`：`MultiAgentExecutionStrategy`、`LocalChildRunScheduler`、Worker 分配与 Reviewer 阶段。
- `src/axiom/runtime/tasks.py` 与 `src/axiom/plan/planner.py`：任务、计划和依赖数据。
- `src/axiom/tools/executor.py`：Tool 读写调度与执行。

### 持久化与测试证据

- 父子 Run 都通过 `src/axiom/runtime/durable.py` 推进；计划和编排状态进入 Checkpoint，而不是只保存在 Python 栈里。
- `tests/test_durable_plan_runtime.py`、`tests/test_durable_multi_agent_runtime.py`：计划、多 Agent 与恢复语义。
- `tests/test_durable_multi_agent_parallelism.py`：Child Run 并发边界。
- `tests/test_tools.py`：Tool 的并发、串行、审批和错误行为。
- `benchmarks/results/tool-concurrency-windows-py312.json`：固定 4 Tool、每个 200ms、5 次预热、30 次正式运行的证据。

## 6. 必须讲清的技术点

1. **ReAct**：模型交替进行推理、行动和观察。适合下一步依赖最新 Tool 结果的开放式任务。
2. **DAG**：有向无环图，用边表达依赖。它让系统识别哪些节点可以同时执行，也能明确阻塞来源。
3. **就绪节点**：自身未完成且所有依赖已完成的节点。调度器只从该集合取任务。
4. **Child Run**：父任务拆出的可独立持久化执行单元；它有自己的状态、Checkpoint 和资源消耗，但预算可归集到根 Run。
5. **有界并发**：并发数有明确上限，避免同时启动过多 LLM、Tool 和状态对象。
6. **异步不等于并行计算**：`asyncio` 擅长 I/O 等待复用；CPU 密集任务需要进程池、原生并行或外部服务。

## 7. 设计理由、替代方案与取舍

为什么不只保留 ReAct？因为复杂任务缺少显式依赖和阶段边界。为什么不全部 Plan-and-Execute？因为小任务的规划成本可能超过收益，而且错误计划会把错误结构化。为什么 Worker 不直接是普通协程？因为 durable Child Run 能保存状态、独立失败、被取消和恢复。

替代方案可以是成熟工作流引擎或分布式队列，但当前目标是本地或企业内部 Runtime。进程内调度器配合 SQLite 更容易验证语义，运维成本也更低。未来规模扩大时，策略和 Run 状态机仍可复用，所有权和排队层再外移。

## 8. 故障与边界场景

- 计划有环：应在进入调度前拒绝，不能让父 Run 永久等待。
- 依赖任务失败：下游任务不能盲目启动；可以标记跳过、触发重规划或让父 Run 失败。
- 某个 Child Run 卡死：由超时、预算和无进展检测终止或给出恢复提示，父 Run 再做对账。
- Reviewer 反复拒绝：必须受步骤、Token、时间和无进展预算约束。
- 写 Tool 混入并发批次：仍由 Tool 元数据和执行器强制串行，不能只信 Planner 的描述。
- 进程退出：当前可从 Checkpoint 恢复执行状态，但调度所有权仍是进程本地；多机接管属于未来架构。

## 9. 分级面试题库（19 题）

### P0 — 简历直击题（9 题）

#### P0-1｜“你先把 ReAct 的完整循环讲一下，它什么时候停止？”

**面试官为什么问：** 简历第一项就是 ReAct。面试官想确认你实现的是可控循环，而不是只会解释“思考—行动—观察”三个词。

**先给结论：** ReAct 每轮把当前上下文交给模型，模型要么给最终答案，要么生成 Tool Call；Runtime 执行 Tool，把结果作为观察追加后进入下一轮。它在模型不给 Tool、出现终态错误、被取消/中断、预算耗尽或无进展时停止。

**60～120 秒完整口语答案：** 在 Axiom 里，一轮先构造模型可见的消息和工具定义，再消费流式返回。如果模型只输出答案，本次 Run 可以完成；如果输出 Tool Call，就先保存待执行状态，经过权限判断和 ToolExecutor 执行，把 ToolResult 追加回消息，再让模型根据新证据继续。停止不能只由模型决定：最大步骤、Token/时间/成本预算、取消、中断、审批等待和无进展检测都会让 Runtime 收敛到明确状态。这里要区分轻量 CLI ReAct 与 Durable Runtime；简历中的恢复和治理能力指后者。

**第一轮追问：**

- **追问：** “模型一直调用工具，不肯结束怎么办？”
  - **回答思路：** 先讲硬预算，再讲语义无进展检测。
  - **30～60 秒可直接说出的答案：** “我不会只依赖模型自觉终止。步骤、模型调用、Tool 调用、Token、时间和成本都有硬上限；同时 ProgressDetector 会识别重复动作、重复错误和短循环，先给一次恢复提示，继续没有新证据就以 NO_PROGRESS 结束。”

**第二轮追问：** “固定 `max_steps` 不就够了吗？”——不够。它只能限制最坏资源，无法区分第 12 步仍在推进和第 4 步已经循环；所以硬上限与进展判断要同时存在。

**常见坑：** 把 ReAct 说成无限 while loop；说“模型输出 final 就证明业务完成”。

**项目证据：** `src/axiom/runtime/strategies.py`、`src/axiom/runtime/durable.py`、`tests/test_progress_detection.py`。

#### P0-2｜“ReAct、Plan-and-Execute 和 Multi-Agent，你到底怎么选？”

**面试官为什么问：** 检查三种模式是不是为了堆名词，以及你是否理解额外模型调用和协调成本。

**先给结论：** 选择依据是任务结构，不是模式越复杂越好。短而开放的任务用 ReAct；依赖明确、可拆成阶段的任务用 Plan-and-Execute；真正存在独立子问题和复核需求时才用 Multi-Agent。

**60～120 秒完整口语答案：** 比如定位一个函数，ReAct 边搜索边回答最直接；跨多个模块做修改，可以先生成 DAG，把独立调研并发、修改和验证按依赖串起来；如果需要不同 Worker 独立研究、再由 Reviewer 检查，才值得用多 Agent。不能全部 ReAct，因为复杂任务的依赖和完成状态不透明；也不能全部先 Plan，因为小任务的规划开销可能超过执行，计划还可能先验错误。Axiom 让三种策略复用同一个 Durable Runtime，因此差别集中在“下一步怎么决定”，不是复制三套治理系统。

**第一轮追问：**

- **追问：** “Multi-Agent 一定比单 Agent 效果好吗？”
  - **回答思路：** 从可分解性、协调成本和质量证据回答。
  - **30～60 秒可直接说出的答案：** “不一定。子问题不独立时，多 Agent 会复制上下文、增加 LLM 调用和合并冲突，可能更慢更贵。我只会在工作可并行、角色边界清楚且 Reviewer 能提供额外验证时使用，并用任务成功率和 cost per success 比较，而不是凭 Agent 数量判断。”

**第二轮追问：** “为什么不全部先 Plan？”——Planner 也是概率模型；计划可能过度拆分或遗漏依赖，小任务还多一次模型延迟，所以策略选择要服从工作负载。

**常见坑：** 声称 Multi-Agent 天然更智能；把三种模式说成三个独立产品。

**项目证据：** `src/axiom/runtime/strategies.py`、`src/axiom/runtime/plan_strategy.py`、`src/axiom/runtime/multi_agent_strategy.py`。

#### P0-3｜“Planner、Worker、Reviewer 各干什么？必须是三个模型吗？”

**面试官为什么问：** 判断所谓多 Agent 是真正的角色与状态分工，还是把同一个 Prompt 调三遍。

**先给结论：** 它们是职责角色，不天然等于三个不同模型。Planner 负责拆分和依赖，Worker 执行独立任务，Reviewer 检查证据与问题；可按成本和能力给角色选择同一或不同模型。

**60～120 秒完整口语答案：** Axiom 当前把 Planner 和 Reviewer 作为父 Run 的串行阶段，把 Worker 分配变成 durable Child Run。Planner 输出可执行分配，Worker 使用工具产生结果，Reviewer 判断是否接受并给出问题，最后父 Run 汇总。角色分开是为了约束输入输出和失败处理，而不是必须部署三个 provider。实践中可以让 Planner/Reviewer 用强模型、简单 Worker 用快模型，也可以全部使用同一模型；这属于路由策略，不能和 Agent 架构混为一谈。

**第一轮追问：**

- **追问：** “Reviewer 不通过以后怎么办？”
  - **回答思路：** 先讲当前有界 Worker 重做，再明确 Reviewer 不是确定性完成门禁。
  - **30～60 秒可直接说出的答案：** “当前 Reviewer 拒绝后会保存 issues，把 assignment 重置为待执行并启动新的 Worker Child Run，最多使用配置的 assignment retry allowance；额度用完就不再重做，父流程会带着 review 证据继续汇总。它避免无限 review，但不等于拒绝必然让整棵 Run 失败；可确定任务的硬门禁应交给测试或 Completion Contract。”

**常见坑：** 说 Reviewer 一定正确；把角色数等同于模型实例数。

**项目证据：** `MultiAgentExecutionStrategy._review()`、`_synthesize()`、Multi-Agent Runtime tests。

#### P0-4｜“为什么计划要用 DAG？什么叫就绪节点？”

**面试官为什么问：** 简历直接写了 DAG 和就绪节点，面试官会检查你是否真正理解依赖，而不是把任务列表换了名字。

**先给结论：** DAG 用有向边表达前置依赖，比普通列表多了并行机会和阻塞原因。一个未完成节点只有在所有依赖都完成时才是就绪节点。

**60～120 秒完整口语答案：** 普通列表只能表达固定顺序，要么全部串行，要么由代码临时猜哪些可并发。DAG 可以表示 A、B 独立，C 同时依赖 A、B：调度器先启动 A、B，二者都完成后 C 才就绪。每次状态变化都重新计算就绪集合，并受并发容量限制。图必须无环，否则会出现未完成节点存在但永远没有就绪节点。节点失败时，其下游不能误启动，应按策略跳过、重规划或让计划失败。

**第一轮追问：**

- **追问：** “环怎么检测？”
  - **回答思路：** 入度拓扑排序或 DFS 颜色标记；把验证放在执行前。
  - **30～60 秒可直接说出的答案：** “可以用 Kahn 拓扑排序：统计每个节点入度，不断取入度为零的节点并删除其出边；最后处理节点数少于总节点数就说明有环。运行时再保留防御检查：有未完成任务但就绪集合为空时不能永久等待。”

**第二轮追问：** “多个节点同时完成，父任务怎么汇合？”——父 Run 读取 Child Checkpoint 做 reconciliation，只有所有必要依赖形成稳定终态才开放下游；汇合是业务状态对账，不只是等待协程返回。

**常见坑：** 把 DAG 解释成 `gather`；漏掉环、失败依赖和恢复后的重新计算。

**项目证据：** `src/axiom/plan/planner.py`、`LocalPlanTaskScheduler`、`PlanExecuteStrategy._reconcile_children()`。

#### P0-5｜“Planner 一开始就规划错了，已经做完的工作怎么办？”

**面试官为什么问：** 检查计划是否可演进，以及重规划会不会浪费或重复副作用。

**先给结论：** 计划不是不可变真理。发现缺失依赖或任务失败后可以有限次重规划，但要按稳定任务语义复用仍有效的完成结果，而不是全盘重跑。

**60～120 秒完整口语答案：** 父 Run 保存当前计划、完成历史和 Child 状态。重规划时把已完成任务作为事实输入；当前实现只对规范化后描述完全相同的新旧任务复用结果和 Child 身份，而不是做语义相似匹配。未启动节点可以释放，失败依赖的下游不应启动。因为重规划本身也是模型调用，所以有次数和预算上限，并把新计划持久化，避免重启后回到旧计划。

**第一轮追问：** “怎么保证复用没有复用错？”——当前规范化描述完全匹配比模糊语义匹配保守，但仍不能证明依赖和外部状态没变；带副作用或时效性的结果仍应重新验证，这是现有复用规则的边界。

**常见坑：** 规划错后全部重跑；或仅按 task index 复用结果。

**项目证据：** `PlanExecuteStrategy._replan()`、`_reuse_completed_tasks()`、plan runtime tests。

#### P0-6｜“为什么这里用 asyncio？线程池不行吗？”

**面试官为什么问：** 这是后端基础题，检查你是否理解 Agent 主要在等待网络 I/O，以及协程、线程和进程的适用边界。

**先给结论：** asyncio 适合大量 LLM、HTTP、MCP 等等待型任务，用较少线程管理很多挂起操作；线程池适合包装同步阻塞库；真正 CPU 密集任务更适合进程池或独立 Worker。

**60～120 秒完整口语答案：** Agent 的大部分时间不是在 Python 里计算，而是在等模型首字、Tool 网络响应或磁盘。协程遇到 `await` 主动把控制权交给事件循环，让它推进其他就绪任务，因此减少串行空等。线程也能做 I/O 并发，而且接同步库更方便，但线程切换、栈内存和共享状态管理更重；Python CPU 密集代码还受 GIL 影响。Axiom 的调度主要是异步 I/O，所以 asyncio 合适，但如果 Tool 连续做两秒 CPU 计算，它会堵住事件循环，需要移到进程池或外部服务。

**第一轮追问：**

- **追问：** “asyncio 是并发还是并行？”
  - **回答思路：** 单线程交错推进属于并发，不是同一时刻多核执行 Python 字节码。
  - **30～60 秒可直接说出的答案：** “默认是并发。事件循环在一个任务等待时切到另一个任务，让多个 I/O 操作处于进行中；这不等于多个 CPU 核同时计算。真正并行需要多进程、释放 GIL 的原生代码或独立 Worker。”

**第二轮追问：** “同步阻塞 Tool 会怎样？”——它不交还控制权，会让同一事件循环上的 LLM、取消和其他 Tool 一起延迟；可用 `asyncio.to_thread` 过渡，CPU 密集则用进程或外部 Worker。

**常见坑：** 说 asyncio 会自动用满多核；把线程池描述成一定更慢。

**项目证据：** 两个 Local Scheduler 的 async `execute()`、`ToolExecutor.execute_all()`、parallelism tests。

#### P0-7｜“coroutine、Task、Future 和 Event Loop，你能用项目解释吗？”

**面试官为什么问：** 你主动说了 asyncio，面试官会顺势验证底层概念是否扎实。

**先给结论：** coroutine 是尚待推进的异步计算；Task 把 coroutine 注册到事件循环并跟踪结果；Future 表示未来会得到的结果，是更底层的等待对象；Event Loop 负责调度就绪回调和 I/O 完成事件。

**60～120 秒完整口语答案：** 调用 `async def` 只得到 coroutine，它还没有自动并发运行。用 `create_task` 才把它包装成 Task 交给事件循环；Task 自身也是 Future 的一种，完成后提供结果或异常。Event Loop 检查哪些 I/O 已就绪、哪些 Task 可以继续，然后运行到下一次 `await`。在 Axiom 中，Child Run 或只读 Tool 会成为受控 Task；Runtime 仍要把业务状态写进 Checkpoint，因为 Task/Future 都只是进程内对象，重启后不存在。

**第一轮追问：** “Task 取消就一定立刻停止吗？”——不一定，取消通过在下一次可取消点抛出 `CancelledError` 协作完成；同步阻塞或不传播取消的外部操作不会立刻停。

**常见坑：** 说 coroutine 创建后立即执行；把 Future 当线程。

**项目证据：** `src/axiom/runtime/supervisor.py`、策略 scheduler 与 cancellation tests。

#### P0-8｜“`create_task`、`gather`、`wait`、`Semaphore` 分别解决什么？”

**面试官为什么问：** 检查你是否只会套一个 `gather`，而不知道任务生命周期、部分完成和有界并发。

**先给结论：** `create_task` 启动并持有任务；`gather` 聚合一组结果；`wait` 更适合观察部分完成或按条件返回；`Semaphore` 限制同时进入关键区的任务数。它们解决不同层面，不能互相替代。

**60～120 秒完整口语答案：** 对固定、独立的小批调用，`gather` 很方便，但它不会自动限制并发，也不表达 DAG。调度器通常先根据依赖选出就绪节点，再用 Task 启动；需要边完成边补充新任务时用 `wait` 或等价机制收割已完成集合；用 Semaphore 或 scheduler capacity 把活跃数限制在安全范围。无论使用哪个原语，都要读取异常、取消剩余任务并保存业务状态，避免出现 orphan Task。

**第一轮追问：** “`gather` 中一个任务失败怎么办？”——取决于异常策略；Agent 编排通常不能让异常悄悄丢失，要将每个 Child 失败持久化，再由父策略决定是否继续，而不是只接受默认 fail-fast。

**常见坑：** 认为 `gather` 自带并发上限或依赖调度。

**项目证据：** `LocalPlanTaskScheduler.execute()`、`LocalChildRunScheduler.execute()`。

#### P0-9｜“为什么只读 Tool 才并发？为什么上限写 4？”

**面试官为什么问：** 简历同时有安全规则和量化配置，面试官会问依据及可配置性。

**先给结论：** 当前默认只并发同时声明只读且并发安全的 Tool，写操作串行以减少顺序冲突和恢复歧义。4 是固定基准和本地默认中的保守上限，不是普适最优值，真实部署要受最小下游容量约束。

**60～120 秒完整口语答案：** 两个代码搜索通常可以同时等待，但两个写文件可能覆盖同一目标，两个 Shell 命令还可能共享工作区状态。仅“只读”也不够：某些读接口使用非线程安全客户端，或会打满同一个服务，因此 Tool 还要显式声明并发安全。上限 4 让本地收益可测且避免无限扇出；如果 LLM 或 MCP 只允许 2 并发，全局有效上限应降到 2 或分依赖配额。当前写串行是安全、可解释的默认，未来只有证明资源不相交并有冲突控制时才放宽。

**第一轮追问：** “两个不同文件的写操作能并行吗？”——理论上可以，但要先规范化资源键、处理 rename/symlink 和事务边界；当前元数据不足以证明完全不相交，所以保守串行。

**第二轮追问：** “Child Run 4 并发，每个又 4 个 Tool，会怎样？”——最坏可能放大到 16 个下游调用，因此局部 Semaphore 不等于全局治理；需要 root/依赖级共享 limiter。

**常见坑：** 把只读等同于线程安全；说 4 是根据 CPU 核心数决定。

**项目证据：** `src/axiom/tools/base.py`、`src/axiom/tools/executor.py`、Tool benchmark。

### P1 — 回答后的自然深挖（6 题）

#### P1-1｜“你说父 Run 会汇合，Child Run 和普通 asyncio Task 有什么本质区别？”

**为什么会被追到这里：** 你在 DAG 或 Multi-Agent 回答中主动提到了 Child Run。

**30～60 秒回答：** asyncio Task 是当前进程里的调度对象；Child Run 是有稳定 ID、父子关系、Checkpoint、ToolExecution 和 Trace 的业务执行单元。Task 消失后，父 Run仍能从 Child Checkpoint 对账和恢复，所以两者不能混为一谈。

**典型继续追问：** “进程重启后谁重新启动 Child？” **回答思路：** 当前由本地调用链恢复父 Run 并 reconciliation；自动多机接管尚未实现。**项目边界：** durable Child 语义已实现，分布式 ownership 未实现。

#### P1-2｜“Reviewer 连续不通过，怎样避免评审死循环？”

**为什么会被追到这里：** 你说 Reviewer 会反馈修正。

**30～60 秒回答：** 当前会持久化 review issues，并在 allowance 内把 assignment 交给新的 Worker Child Run；额度耗尽后停止重做并进入后续汇总，而不是无限否决。整个过程仍受 step/token/time 与无进展控制。Reviewer 是模型反馈角色，不是确定性验收契约。

**典型继续追问：** “Reviewer 本身判断错呢？” **回答思路：** 对可验证任务优先测试/不变量，Reviewer 只是补充信号。**项目边界：** 当前 Reviewer 是模型角色，不是通用确定性 verifier。

#### P1-3｜“某个 Tool 内部用了同步 HTTP 客户端，会发生什么？”

**为什么会被追到这里：** 你强调事件循环适合异步 I/O。

**30～60 秒回答：** 同步调用会占住事件循环线程，使同 loop 的其他 Run、取消和超时都不能及时推进。短期可放到线程池包装，长期改为异步客户端；若是 CPU 密集则用进程池或独立 Worker。

**典型继续追问：** “怎么发现？” **回答思路：** 看 event-loop lag、慢 Span 和 Task 堆栈。**项目边界：** 当前没有完整生产 loop-lag 告警。

#### P1-4｜“并发越高是不是越快？”

**为什么会被追到这里：** 你展示了 4 并发的基准收益。

**30～60 秒回答：** 只在工作独立且下游未饱和时成立。并发继续升高可能触发 LLM 429、连接池等待、SQLite 写竞争、内存上涨和调度开销，使 P99 反而变差；上限应通过负载测试寻找拐点。

**典型继续追问：** “看什么指标调上限？” **回答思路：** 吞吐、P95/P99、错误率、队列、下游利用率与成本。**项目边界：** 当前 4 是本地策略/基准，不是线上容量结论。

#### P1-5｜“下游模型只能接受 2 个并发，但 Worker 上限是 4，限制放哪？”

**为什么会被追到这里：** 你区分了 Child、Tool 和依赖容量。

**30～60 秒回答：** 局部 Worker 上限只能控制本层；还需要按 provider 共享的并发 limiter，实际放行取各层约束的交集。共享服务再加租户和全局配额，避免多个根 Run 各自以为还有容量。

**典型继续追问：** “会不会饿死低优先级任务？” **回答思路：** 有界队列、公平调度和优先级老化。**项目边界：** 全局模型配额属于未来架构。

#### P1-6｜“写操作为什么不按文件锁并发？”

**为什么会被追到这里：** 你说不同资源理论上可以并行。

**30～60 秒回答：** 可以演进，但资源键不只是字符串路径，还要处理规范化、symlink、目录级操作、Shell 的隐式副作用和跨 Tool 冲突。当前串行牺牲一些吞吐，换取简单、可验证的顺序语义。

**典型继续追问：** “什么时候值得做？” **回答思路：** 写调用占主路径且有可靠资源声明时。**项目边界：** 当前没有细粒度写资源锁管理器。

### P2 — 攻击、边界与架构取舍（4 题）

#### P2-1｜“你说性能提升 74%，到底测了什么？”

**最安全的结论：** 这是 ToolExecutor 固定等待型 I/O 的单批延迟降低，不是完整 Agent 性能或线上吞吐提升。

**回答主线：** 4 个 Tool 各等待 200ms；串行理论约 800ms，并发理论约 200ms，所以理想降低约 75%。实际 30 次平均 828.203ms 对 207.773ms，改善 74.99%；调度、计时和系统噪声使它不严格等于 75%。

**不要说什么：** “Agent 整体快了 74%”或“吞吐提高 4 倍”。

**如果继续扩展怎么做：** 增加真实 Tool、不同并发、端到端任务成功率、Token、P99 和资源利用率基准。

#### P2-2｜“如果模型调用占总延迟 90%，这个并发优化还有意义吗？”

**最安全的结论：** 端到端收益会很小，优化应先看延迟拆解。

**回答主线：** 只有请求内存在多个独立 Tool 且 Tool 是显著瓶颈时才有价值；否则优先优化模型选择、Prompt、TTFT 或跨请求吞吐。

**不要说什么：** 为维护简历数字而声称任何场景都收益明显。

**如果继续扩展怎么做：** 以 Trace 做 Amdahl 式瓶颈分析，并用端到端 benchmark 复测。

#### P2-3｜“如果 Multi-Agent 比单 Agent 更慢、更差，你还会用吗？”

**最安全的结论：** 不会默认使用，策略应按任务类别和证据选择。

**回答主线：** 对不可分解任务回退 ReAct；对协调开销高的任务减少 Worker；用多 trial 成功率、成本和延迟比较，而不是追求架构复杂度。

**不要说什么：** “多 Agent 总会涌现更好能力。”

**如果继续扩展怎么做：** 建立按任务特征的 strategy router 和离线/在线评测。

#### P2-4｜“这套调度能直接扩成多机吗？”

**最安全的结论：** 策略状态和 Checkpoint 可复用，但当前 scheduler 与 Active Run 所有权是进程本地。

**回答主线：** 多机需要 durable queue、原子 claim、Worker lease/heartbeat、fencing 和共享配额；Kubernetes 只管理进程，不解决 Run 所有权。

**不要说什么：** 把 `asyncio`、CAS 或 Supervisor 称为分布式调度器。

**如果继续扩展怎么做：** 先做 PostgreSQL lease 小原型，再引入有界准入队列。

## 10. 重点追问树（6 条）

### 追问树 1：ReAct 为什么可控

```text
ReAct 完整 loop 是什么？
└─ 模型一直调用 Tool，什么时候停？
   └─ max_steps 不就够了吗？
      └─ 模型输出 final，为什么还不能直接信？
```

- **“什么时候停？”**
  - **面试官意图：** 检查你实现的是受控状态机还是无限 `while`。
  - **回答思路：** 模型终止提议只是一个入口；取消、等待、预算、错误和无进展都是独立控制。
  - **30～60 秒口述：** “每轮模型要么给 Tool Call，要么提出 final。Runtime 还会在取消、interrupt/HITL、步骤或 Token/时间/成本预算、依赖错误和无进展时停止或等待。因此停止不是一个 `if final`，而是状态机对多种控制信号的收敛。”
- **“max_steps 不就够了吗？”**
  - **面试官意图：** 看你能否区分资源上限和进展语义。
  - **回答思路：** max_steps 保证最坏成本；ProgressDetector 提前识别重复动作、错误、短周期和状态停滞。
  - **30～60 秒口述：** “max_steps 只能说最多跑多少步，不能区分第十步仍在获得新证据，还是第三步已经 A-B-A-B。Axiom 保留硬上限，同时用稳定指纹检测无进展，先给一次恢复提示，仍不收敛才 `NO_PROGRESS`。”
- **“final 为什么不能直接信？”**
  - **面试官意图：** 从 Agent loop 追到完成正确性。
  - **回答思路：** 终止提议、确定性契约、最终 Run 状态分层。
  - **30～60 秒口述：** “final 只说明模型不想再调用 Tool。有 Completion Contract 时还要检查 ToolExecution、输出、产物或 Plan/Child 状态；失败可有一次普通预算内纠正。开放任务没有确定性契约时兼容完成，但 verification 是 NOT_APPLICABLE，不能说客观验证过。”

### 追问树 2：为什么不是全用 Multi-Agent

```text
为什么保留 Multi-Agent？
└─ Agent 越多越好吗？
   └─ Planner/Worker/Reviewer 必须用不同模型吗？
      └─ 怎么证明这次拆分值得？
```

- **“Agent 越多越好吗？”**
  - **面试官意图：** 排除“多 Agent 天然涌现”的堆概念回答。
  - **回答思路：** 可分解性收益对比协调、重复 Context、合并冲突和模型成本。
  - **30～60 秒口述：** “不一定。子问题不独立时，多 Worker 会重复搜索、复制 Context，还要付 Planner、Reviewer 和汇总成本。只有工作能独立并行、角色边界清楚、复核能增加证据时才值得；否则 ReAct 往往更快更便宜。”
- **“必须不同模型吗？”**
  - **面试官意图：** 看你是否把角色和模型路由混为一谈。
  - **回答思路：** Agent 是职责与状态边界；模型可以相同，当前没有 Model Router。
  - **30～60 秒口述：** “Planner、Worker、Reviewer 是输入输出契约和职责，不等于三家模型。当前可以使用同一模型；理论上可让复杂规划/复核用强模型、简单 Worker 用便宜模型，但那是另一个经过评测的路由决策，项目没有声称已实现通用 Model Router。”
- **“怎么证明值得？”**
  - **面试官意图：** 要求从设计偏好回到证据。
  - **回答思路：** 同任务、同工具/预算，对照策略，多 trial 看成功率、延迟与 cost per success。
  - **30～60 秒口述：** “我会按任务类型固定数据集，对 ReAct、Plan、Multi-Agent 做独立重复试验，同时看成功率、总延迟、模型调用、Token 和 cost per success。只快但失败更多、或者成功略升但成本翻倍，都不能自动判 Multi-Agent 更好。”

### 追问树 3：DAG 到恢复

```text
为什么计划是 DAG，不是 List？
└─ ready node 怎么定义、环怎么查？
   └─ dependency fail 后怎么办？
      └─ replan 后完成节点如何复用？
```

- **“ready node 和环怎么处理？”**
  - **面试官意图：** 验证 DAG 不是换名的任务数组。
  - **回答思路：** 全部依赖终态成功才 ready；执行前拓扑验证，运行时防御“有未完成但无 ready”。
  - **30～60 秒口述：** “节点只有在所有前置依赖完成后才进入 ready 集合，再受并发上限筛选。计划先用 Kahn 拓扑排序或 DFS 查环；运行时如果还有未完成节点却没有 ready/active 节点，也不能永久等待，要把它识别成无效依赖或失败状态。”
- **“上游失败怎么办？”**
  - **面试官意图：** 检查失败传播是否明确。
  - **回答思路：** 不误启动下游；区分可重试依赖、可重规划任务和必需任务失败。
  - **30～60 秒口述：** “失败节点的下游不能当作 ready。父策略先读取 Child 的结构化终态：暂态、安全的依赖失败可在原预算内恢复，计划错误可有限重规划，必需前置条件失败则跳过受影响节点或让计划失败，不能把局部异常静默变成总任务成功。”
- **“replan 怎么复用？”**
  - **面试官意图：** 看重规划是否导致重复工作或副作用。
  - **回答思路：** 当前按规范化描述完全匹配并复用已完成结果；明确它不证明语义与依赖等价。
  - **30～60 秒口述：** “重规划会把已完成事实带给 Planner。当前代码只在新旧任务的规范化描述完全一致时沿用完成结果、attempt 和 Child 身份，不按数组下标，也不做模糊语义匹配。这个规则可复现但仍可能遇到依赖或外部状态变化，所以带副作用/时效性的结果应重新验证。”

### 追问树 4：asyncio 基础

```text
为什么用 asyncio？
└─ coroutine、Task、Event Loop 是什么关系？
   └─ create_task / gather / wait / Semaphore 怎么选？
      └─ async、thread、process 分别适合什么？
```

- **“coroutine、Task、Event Loop 什么关系？”**
  - **面试官意图：** 检查是否真的理解异步执行模型。
  - **回答思路：** coroutine 是可暂停计算；Task 注册并跟踪；loop 推进 ready Task 与 I/O 回调。
  - **30～60 秒口述：** “调用 `async def` 只得到 coroutine，不会自动并发。`create_task` 把它包装成 Task 交给 Event Loop；Task 运行到 `await` 让出控制权，I/O 就绪后 loop 再恢复它。Task 是进程内对象，所以 Child Run 还必须把业务状态持久化。”
- **“四个 asyncio 原语怎么选？”**
  - **面试官意图：** 排除只会把所有调用塞进 `gather`。
  - **回答思路：** create_task 管生命周期，gather 聚合固定集合，wait 收割部分完成，Semaphore 控容量。
  - **30～60 秒口述：** “DAG 先算 ready 集合，再 `create_task` 启动；需要边完成边补新节点时用 `wait` 一类机制收割；固定独立小批可 `gather`；Semaphore 只限制同时进入数量。它们都不自动表达依赖、错误策略或持久化。”
- **“async、thread、process 怎么选？”**
  - **面试官意图：** 从项目自然追到 I/O/CPU 与 GIL。
  - **回答思路：** 原生异步 I/O 用 asyncio；同步阻塞库用线程过渡；CPU 密集用进程/外部 Worker。
  - **30～60 秒口述：** “LLM、HTTP、MCP 等等待型操作适合 asyncio。同步 SDK 可用线程池避免堵 loop，但共享状态更复杂；纯 Python CPU 密集任务即使用 async 也会占住事件循环，还受 GIL 影响，应放进程池或独立 Worker。asyncio 提供并发，不等于多核并行。”

### 追问树 5：并发上限

```text
为什么最多并发 4 个 Tool？
└─ 只读是否一定 concurrency-safe？
   └─ Child 4 × Tool 4 会不会放大成 16？
      └─ 不同文件的写操作未来能否并行？
```

- **“只读一定安全吗？”**
  - **面试官意图：** 看你是否把业务副作用与实现线程安全混淆。
  - **回答思路：** 同时要求 read-only 和 concurrency-safe；还要受下游容量约束。
  - **30～60 秒口述：** “不一定。一个读 API 也可能复用非并发安全客户端、消耗共享游标或把下游打满，所以 Axiom 要同时声明只读和并发安全。4 只是本地保守上限；如果依赖只允许 2，实际放行必须更小。”
- **“多层并发会不会乘法放大？”**
  - **面试官意图：** 检查局部 Semaphore 的盲区。
  - **回答思路：** Child 与 Tool 是不同层；最坏扇出相乘，当前必须保守，服务化才需要共享依赖限额。
  - **30～60 秒口述：** “会。4 个 Child 各自再开 4 个 Tool，理论上可能形成 16 个依赖请求，所以每层一个 Semaphore 不能代表全局容量。当前本地范围靠保守上限和预算控制；共享服务应按 root/依赖汇总并发，但这不是现有全局 rate limiter。”
- **“不同资源的写能否并行？”**
  - **面试官意图：** 追问保守策略的性能代价。
  - **回答思路：** 理论可行，前提是可靠资源键、规范化、冲突控制和恢复语义。
  - **30～60 秒口述：** “可以作为未来优化，但先要证明资源不相交。文件路径还涉及目录操作、rename、symlink，Shell 有隐式副作用；当前元数据不足时统一串行更安全。只有写成为瓶颈且资源声明可靠，才值得引入细粒度锁。”

### 追问树 6：74% 证据

```text
74% 到底怎么测？
└─ 为什么理论上接近 75%？
   └─ 测的是 latency 还是 throughput？
      └─ LLM 占 90% 时，这个优化还有意义吗？
```

- **“为什么接近 75%？”**
  - **面试官意图：** 验证数字是否能从实验设计推导。
  - **回答思路：** 4 个独立 200ms 等待：串行约 800ms，并发约 200ms，降低约 75%。
  - **30～60 秒口述：** “基准是 4 个各等待 200ms 的独立 Tool。理想串行总时长约 800ms，四并发取最长一个约 200ms，所以节省 `(800-200)/800=75%`。实测 30 次正式运行平均 828.203ms 对 207.773ms，即 74.99%，差异来自调度和系统噪声。”
- **“latency 还是 throughput？”**
  - **面试官意图：** 防止把单批耗时包装成系统容量。
  - **回答思路：** 只测单批完成 latency；没有多用户、饱和、队列或任务质量结论。
  - **30～60 秒口述：** “这个数字是一个固定批次从开始到全部完成的延迟，不是 QPS 或吞吐。它也不含 LLM、进程启动、真实网络和任务成功率，所以我只用它证明 ToolExecutor 的等待型并发路径生效。”
- **“LLM 占 90% 还有意义吗？”**
  - **面试官意图：** 看你是否理解 Amdahl 定律式瓶颈约束。
  - **回答思路：** 端到端上限由可优化部分占比决定；用 Trace 决定优先级。
  - **30～60 秒口述：** “若 Tool 只占总延迟 10%，即便 Tool 无限加速，端到端也最多改善约 10%。这时优化模型 TTFT、Context 或减少调用更重要。并发仍可能改善某些多 Tool 阶段，但不能把 74% 外推到完整 Agent。”

## 11. 面试官攻击面与防守口径

1. **“三种模式是不是堆概念？”** 用任务结构、额外成本和共享 Runtime 回答。
2. **“DAG 不就是 gather？”** 强调依赖、就绪、失败传播和恢复。
3. **“asyncio 是不是多线程？”** 明确单 loop 并发、线程/进程适用边界。
4. **“只读就一定安全吗？”** 明确还需 `is_concurrency_safe` 和下游容量。
5. **“4 是拍脑袋吗？”** 承认它是当前默认/基准条件，生产值靠负载拐点。
6. **“74% 是否夸大？”** 主动限定为固定 Tool I/O 单批延迟。
7. **“Multi-Agent 为什么不总开？”** 用成功率、延迟和成功成本选择。
8. **“能否多机？”** 明确当前本地所有权与未来 lease/queue 分层。

## 12. 当前边界

### 【当前已实现】

ReAct、durable Plan-and-Execute、Planner / Worker / Reviewer；父子 Run；DAG 就绪节点调度；本地有界 Child Run 并发；只读且并发安全 Tool 的有界并发；写 Tool 串行；固定 I/O 基准。

### 【当前部分支持】

并发治理主要是进程内和根 Run 级；Child Run 已有持久化语义，但运行中的协程所有权仍属于当前进程。CLI 的轻量 ReAct 路径并不自动拥有 DurableAgentRuntime 的全部恢复、预算和无进展能力。

### 【未来可扩展】

共享任务队列、分布式 Run 所有权、Worker 租约/心跳、跨进程全局并发、租户优先级、依赖级熔断和自动扩缩容。它们是共享服务演进，不是当前项目“缺基本功能”。

## 13. 面试前 5 分钟速背

### 5 个概念

ReAct；DAG 就绪节点；durable Child Run；有界并发；读写副作用。

### 5 个源码锚点

`runtime/strategies.py`；`runtime/plan_strategy.py`；`runtime/multi_agent_strategy.py`；`tools/executor.py`；`benchmarks/results/tool-concurrency-windows-py312.json`。

### 必须不思考就能回答的 5 道 P0

三种模式为何共存？DAG 如何调度？asyncio 解决什么？写 Tool 为何串行？74% 如何证明且边界是什么？

### 3 个陷阱

不要说多 Agent 必然更好；不要说 asyncio 等于多核；不要把 74% 说成端到端提升。

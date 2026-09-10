# 简历二：Tool Runtime、MCP 与安全

> 本册回答“模型如何安全、统一地调用本地和远程能力”。生产流量治理见第 8 册。

## 0. 简历原句

> 为统一异构工具接入与敏感操作控制，设计 Tool Runtime，通过 ToolRegistry / ToolExecutor 管理文件、Shell、Web 抓取、代码检索及 MCP 工具；基于 MCP SDK 支持 stdio / Streamable HTTP，通过 HITL、路径/命令校验与 JSONL 审计约束敏感操作。

## 1. 这条经历解决了什么问题

模型调用工具并不只是“执行一个 Python 函数”。系统要解决工具发现、Schema 暴露、参数解析、并发、超时、审批、权限、错误归一化和审计。MCP 又把远程服务的生命周期、传输和内容类型带进来。如果每个 Tool 自己处理这些横切逻辑，很快就会出现策略不一致和安全绕过。

Axiom 因此把定义与执行分开：`ToolRegistry` 管理“有哪些能力以及怎样告诉模型”，`ToolExecutor` 管理“这次调用是否允许、怎样执行、超时后返回什么、如何审计”。本地 Tool 和 MCP Tool 在上层共享 `Tool` 抽象，但底层连接和生命周期仍不同。

## 2. 30～45 秒主回答

我把工具系统分为注册层和执行层。Registry 保存 Tool 元数据、JSON Schema 和调用入口，并向模型输出统一的函数定义；Executor 解析模型参数，执行审批与策略校验，再按只读/写入语义调度，统一返回 `ToolResult`。

MCP 侧使用官方 Python SDK，支持本地子进程的 stdio 和远程 Streamable HTTP。发现到的 MCP tools、resources、prompts 被适配成 Axiom Tool，再进入同一个执行器。安全上当前有 HITL、工作区路径限制、危险命令拦截和 JSONL 审计，但这些是防护层，不是操作系统级沙箱；真正多租户时仍要加容器隔离、身份和网络策略。

## 3. 2～3 分钟完整故事

这条经历的起点是统一异构工具。文件读写、Shell、代码检索和 MCP 的调用方式不同，但对模型而言都需要名称、描述、参数 Schema 和结果。因此我定义统一的 Tool 抽象，让 Registry 负责注册、去重和函数定义导出；Executor 负责一次调用的完整生命周期。这样新增 Tool 不需要复制审批、审计和错误处理。

MCP 不是 Function Calling 的同义词。Function Calling 是模型表达“我要调用哪个函数、参数是什么”的接口形式；MCP 是应用与外部能力之间的发现和调用协议；底层使用 JSON-RPC 消息。Axiom 的客户端通过官方 SDK 建立 stdio 或 Streamable HTTP 会话，调用 `list_tools` 并将远端 Tool 包装成本地对象。resources 和 prompts 也通过虚拟 Tool 暴露。返回的富内容会被归一化为文本，这让上层简单，但图片等结构化语义可能损失，必须诚实说明。

安全不能只靠 Prompt。写操作可以触发 HITL；`PathGuard` 将文件路径约束在工作区；`CommandGuard` 拦截明显危险命令；审计日志对部分敏感字段做脱敏。这些机制降低误操作概率，但无法替代容器、只读挂载、低权限身份和网络出口控制，也不能声称消灭了符号链接竞态、TOCTOU 或所有命令绕过。

恢复方面，Durable Runtime 用稳定 invocation ID 和 `ToolExecution` 保存执行状态。已成功的调用恢复时可以复用结果；如果 Worker 在外部写操作成功后、保存成功状态前崩溃，仍存在副作用不确定窗口。因此写 Tool 的长期正确方案是幂等键、查询操作状态或补偿，而不是无脑重试。

## 4. 架构与调用链

```text
LLM tool_call
     ↓
ToolRegistry：名称 / 描述 / 参数 Schema / 元数据
     ↓
ToolExecutor：解析 → 权限/HITL → 路径/命令校验 → 超时/调度 → 审计
     ├─ 本地 Tool：文件 / Shell / Web / Code Search
     └─ MCP Adapter
            ↓
       官方 MCP SDK
       ├─ stdio 子进程
       └─ Streamable HTTP 服务
```

## 5. 最新源码实现

- `src/axiom/tools/base.py`：`Tool`、`ToolContext`、`ToolResult` 与 Schema 辅助。
- `src/axiom/tools/registry.py`：`ToolRegistry` 注册、查询和模型定义导出。
- `src/axiom/tools/executor.py`：`ToolExecutor.execute_all/execute_one`，审批、策略、调度和错误归一化。
- `src/axiom/tools/builtins.py`：文件、Shell、Web、记忆、技能、代码检索等内置工具。
- `src/axiom/mcp/config.py`：MCP server spec 配置。
- `src/axiom/mcp/client.py`：`McpClientManager`，stdio/Streamable HTTP 会话，tools/resources/prompts 适配。
- `src/axiom/mcp/server.py`：简化 MCP-like server；不能宣传为完整通用服务端实现。
- `src/axiom/policy/path_guard.py`、`command_guard.py`、`permissions.py`、`audit_log.py`：路径、命令、权限和 JSONL 审计。
- `src/axiom/runtime/durable.py` 与 `runtime/checkpoints.py`：稳定 invocation ID、ToolExecution 状态和恢复复用。
- `tests/test_tools.py`、`tests/test_mcp.py`、`tests/test_policy.py`、`tests/test_durable_runtime.py`：可执行证据。

## 6. 必须讲清的技术点

**MCP** 解决应用如何发现和调用外部 tools/resources/prompts；**Function Calling** 是 LLM 输出结构化工具意图的形式；**JSON-RPC** 是 MCP 消息层采用的远程调用格式。三者层级不同。

**stdio** 由客户端拉起本地子进程并通过标准输入输出通信，部署简单、权限继承明显；**Streamable HTTP** 面向网络服务，需要处理鉴权、连接、超时和服务端容量。它不是旧式 SSE transport 的简单别名。

**HITL（Human in the Loop，人在回路）** 是在敏感操作前让人确认，但审批必须绑定具体 Tool、参数和版本，否则审批后参数变化会形成绕过。

**路径保护** 只验证目标是否在允许工作区；**命令保护** 是规则拦截；两者都不是沙箱。沙箱是在操作系统边界限制文件、进程、网络和资源，即使应用层判断失误也限制破坏范围。

## 7. 设计理由、替代方案与取舍

Registry 和 Executor 分离是为了让能力描述与运行策略独立演进。若合在 Tool 内部，200 个 Tool 会复制 200 份审批与审计逻辑。代价是执行上下文和元数据契约要保持一致。

将 MCP Tool 适配为统一 Tool 减少上层复杂度，但归一化会丢失部分富内容，远端错误也可能被压缩成文本。大规模 Tool 集合还不能全部塞进 Prompt，未来需要候选召回、分组与按需发现。

当前采用应用层 guard 符合本地开发者工具的范围。共享企业服务时，应用层判断仍保留，但真正安全边界应下沉到每 Run 沙箱、最小权限身份、网络出口策略和不可篡改审计。

## 8. 故障与边界场景

- MCP 子进程启动失败：标记该 server 不可用，不能阻塞所有本地 Tool。
- 远端 MCP 超时：按依赖设截止时间；只对安全调用做有限重试，并考虑熔断。
- 参数 JSON 不完整：返回结构化参数错误给模型纠正，不能把解析异常当进程异常。
- Tool 名称冲突：注册阶段拒绝或使用稳定命名空间。
- 写操作返回超时：执行结果可能未知，先查状态或依赖幂等键，不能直接再写一次。
- 审计写入失败：当前不等于强一致合规审计；高风险环境应决定 fail-open 还是 fail-closed。
- 恶意网页提示注入：外部内容是数据，不应自动获得工具权限；高风险动作仍走策略和审批。

## 9. 分级面试题库（20 题）

### P0 — 简历直击题（9 题）

#### P0-1｜“从模型决定调用 Tool，到结果回到模型，完整链路是什么？”

**面试官为什么问：** 简历写了 Tool Runtime。面试官要确认你理解的是一条受治理的执行链，而不是把函数列表传给模型。

**先给结论：** Runtime 先从 Registry 导出模型可见的 Tool Schema；模型返回名称和参数；Executor 解析并校验，执行权限/HITL、读写调度、超时和审计；结果统一成 ToolResult，追加回消息让模型继续判断。

**60～120 秒完整口语答案：** Axiom 启动时把内置 Tool 和发现到的 MCP Tool 注册到 ToolRegistry。每次模型调用只看到允许暴露的名称、描述和 JSON Schema。模型流式返回 Tool Call 后，Runtime 合并参数并先持久化 pending state；Executor 查找 Tool、解析 JSON、执行权限和审批，再调用本地函数或 MCP adapter。成功和失败都归一化成 ToolResult，Durable 路径还记录 ToolExecution 和 Span，最后将结果作为观察回给模型。未知 Tool、非法 JSON 或业务错误不会让整个进程直接崩掉，而是形成结构化错误，是否让模型纠正由 Run 状态和预算决定。

**第一轮追问：**

- **追问：** “Tool 返回错误以后 Agent 怎么办？”
  - **回答思路：** 区分可纠正参数错误、短暂依赖错误和终止错误。
  - **30～60 秒可直接说出的答案：** “参数错误可以作为 ToolResult 返回，让模型基于明确原因修正一次；网络暂态错误只对安全调用有限重试；权限拒绝、预算耗尽或持续失败则进入等待或终态。错误反馈也受步骤和无进展限制，不能形成无限自我纠错。”

**第二轮追问：** “流式 Tool 参数截断怎么办？”——只有收到完整结束事件并能解析 JSON 才执行；参数不完整应返回解析错误，绝不能猜参数后执行写操作。

**常见坑：** 把 Tool Call 描述成模型直接运行代码；遗漏权限、持久化和结果回注。

**项目证据：** `src/axiom/agent/query.py`、`src/axiom/runtime/durable.py`、`src/axiom/tools/executor.py`。

#### P0-2｜“模型怎么知道有哪些 Tool？Tool Schema 到底有什么用？”

**面试官为什么问：** 检查 Function Calling 的基本机制和 Schema 的能力边界。

**先给结论：** Registry 把 Tool 名称、描述和 JSON Schema 转成模型函数定义。Schema 帮模型理解参数结构，也支持执行前校验，但它不能保证模型一定选对 Tool 或参数一定符合业务语义。

**60～120 秒完整口语答案：** Tool Schema 描述字段、类型、必填项和枚举等约束，让模型产生更稳定的结构化调用，也让 Runtime 在副作用发生前拒绝明显非法输入。它比把用法写在一段自然语言 Prompt 里更机器可读。但 Schema 解决不了“该不该调用”“路径是否越权”“金额是否合理”等上下文策略，所以还需要权限、PathGuard、CommandGuard 和业务校验。当前 Registry 负责定义导出，Executor 负责真正执行，职责不能倒过来。

**第一轮追问：** “参数少了或类型错了怎么办？”——执行前拒绝并返回可解释错误，允许模型在预算内纠正；写操作不能宽松猜测。

**常见坑：** 说 JSON Schema 能保证业务安全；把 Tool 描述完全交给模型自由解释。

**项目证据：** `src/axiom/tools/base.py`、`src/axiom/tools/registry.py`、`tests/test_tools.py`。

#### P0-3｜“为什么要拆 ToolRegistry 和 ToolExecutor？”

**面试官为什么问：** 简历直接点名两个组件，面试官会检查分层是否有真实价值。

**先给结论：** Registry 管“有哪些能力以及怎样描述”，Executor 管“这次调用能不能、怎么运行”。分开后本地和 MCP Tool 能复用同一套审批、超时、并发和审计策略。

**60～120 秒完整口语答案：** 如果每个 Tool 自己处理权限、超时和日志，几十个工具很快会产生不一致：有的漏审批，有的错误格式不同，有的无限等待。Registry 只保存名称到 Tool 的映射并导出 Schema；Executor 查找 Tool 后统一解析、授权、调度和归一化结果。这样新增 Tool 主要实现能力本身，横切策略集中治理。代价是 Tool 元数据和 ToolContext 契约要清楚，不能让 Tool 绕开 Executor 私自执行。

**第一轮追问：** “为什么不做一个大 Router？”——Registry 是确定性能力目录，不负责用另一个模型决定路由；Tool 候选很多时可在其前面增加检索，但最终权限仍由 Executor 判断。

**常见坑：** 说 Registry 是数据库、Executor 是线程池；只讲代码整洁，不讲策略一致性。

**项目证据：** `src/axiom/tools/registry.py`、`src/axiom/tools/executor.py`。

#### P0-4｜“MCP 到底解决什么？和 Function Calling、REST、JSON-RPC 分别什么关系？”

**面试官为什么问：** MCP 是简历最显眼名词之一，也最容易被概念混淆。

**先给结论：** Function Calling 是模型表达结构化调用意图；MCP 是应用发现和调用外部 tools/resources/prompts 的协议；JSON-RPC 是 MCP 消息格式基础。REST 是另一种面向 HTTP 资源/接口的集成方式，可以被 Tool 包装，但不提供 MCP 的统一发现语义。

**60～120 秒完整口语答案：** 一个内部 REST API 完全可以手写成 Axiom Tool，所以 MCP 不是所有集成都必须使用。它的价值在于服务端用统一协议公开能力、资源和提示模板，客户端通过 SDK 发现和调用，减少每个系统自定义适配。发现后的 MCP Tool 仍要被转换成模型可见的 Function Calling Schema；模型选中后，Runtime 再通过 SDK 发 JSON-RPC 请求。Axiom 主要复用官方 MCP SDK，没有声称自己重写完整协议栈；项目中的 server 也是简化实现。

**第一轮追问：**

- **追问：** “既然能直接写 HTTP Tool，为什么还要 MCP？”
  - **回答思路：** 标准发现与生态复用，对比自定义 API 的直接性。
  - **30～60 秒可直接说出的答案：** “单个稳定 API 直接封装最简单；当能力来自多个团队、需要动态发现 tools/resources/prompts 时，MCP 能统一契约和 SDK。它减少适配，不自动解决鉴权、配额和业务安全，所以不是为了追新协议而全部改成 MCP。”

**第二轮追问：** “MCP 和普通 RPC 的本质区别？”——MCP 面向模型上下文与工具生态定义了能力发现、内容和调用约定；底层仍依赖具体 transport 和 RPC 消息。

**常见坑：** 把 MCP、Function Calling 和 JSON-RPC 说成同一个东西；声称 MCP 替代所有 REST。

**项目证据：** `src/axiom/mcp/client.py`、`src/axiom/mcp/server.py`。

#### P0-5｜“stdio 是怎么通信的？MCP 子进程挂了怎么办？”

**面试官为什么问：** 简历明确写了 stdio，面试官会从协议追到进程生命周期和故障隔离。

**先给结论：** 客户端拉起本地 server 子进程，通过标准输入输出交换协议消息；stderr 用于诊断。子进程退出、握手失败或调用超时应只让该 server/Tool 失败，不能拖垮整个 Runtime。

**60～120 秒完整口语答案：** stdio 的优点是本机部署简单，不需要占端口，客户端能管理进程生命周期；缺点是权限通常继承宿主、进程启动有成本，stdout 还必须保持协议干净。Axiom 通过 MCP SDK 建立会话并发现 Tool。若 server 启动失败或运行中退出，调用应得到结构化错误，并受超时与 RunBudget 约束。生产化还可以做健康状态、有限重启和 per-server 隔离，但不能无限拉起造成重启风暴。

**第一轮追问：** “Server 把日志写到 stdout 会怎样？”——可能污染协议帧，所以诊断日志应走 stderr 或独立日志通道。

**常见坑：** 说 stdio 没有网络就没有安全风险；忽略子进程权限和资源继承。

**项目证据：** `McpClientManager._session()`、`tests/test_mcp.py`。

#### P0-6｜“Streamable HTTP 和 stdio 怎么选？它就是 SSE 吗？”

**面试官为什么问：** 检查 transport 选择和 HTTP 基础，不是考名词背诵。

**先给结论：** stdio 适合同机、单客户端管理的工具；Streamable HTTP 适合独立部署和多客户端访问，但需要鉴权、连接复用和容量治理。Streamable HTTP 不是“传统单向 SSE”的简单同义词。

**60～120 秒完整口语答案：** 选择看部署边界。CLI 拉起本地代码分析器时，stdio 配置少、生命周期明确；企业内部共享能力时，HTTP 便于独立扩缩容和统一身份，但会增加 DNS/TLS、连接池、超时、限流和服务端排队。MCP Streamable HTTP 定义的是 MCP 在 HTTP 上的请求与流式响应语义，可能使用流式机制，但不能直接等同于旧 SSE transport。Axiom 通过官方 SDK 处理细节，文档只声明实际支持的 transport。

**第一轮追问：** “HTTP 为什么要连接复用？”——重复 TCP/TLS 建连增加延迟和端口压力，连接池能复用，但池过大也会压垮服务端。

**常见坑：** 用“HTTP 更先进”替代权衡；把 Streamable HTTP 描述成 WebSocket。

**项目证据：** `src/axiom/mcp/config.py`、`src/axiom/mcp/client.py`。

#### P0-7｜“MCP 的 tools、resources、prompts 有什么区别？”

**面试官为什么问：** 确认你使用的不只是 `call_tool`，并理解只读内容与可执行动作的语义差异。

**先给结论：** tools 是可带副作用的可执行能力；resources 是可读取的内容或引用；prompts 是服务端提供的提示模板。三者都能进入 Agent，但权限和展示方式不应相同。

**60～120 秒完整口语答案：** Axiom 将远端 tools 直接适配为 Tool，并为 resources/prompts 增加虚拟 Tool 入口，便于现有执行链统一消费。这个做法降低上层复杂度，但也会弱化类型差异，所以策略仍要根据能力元数据判断风险。返回 rich content 当前主要归一化为文本；图片、嵌入资源和大对象可能丢失结构或过度占用 Context，未来应保留 typed envelope 和引用。

**第一轮追问：** “MCP session 为什么不能每次调用都新建？”——握手和进程/连接建立有成本；可复用会话，但要处理失效、并发安全和关闭。

**常见坑：** 说 resources 和 tools 都是普通函数；说所有 MCP 返回天然是字符串。

**项目证据：** `McpClientManager._tools_for_server()`、`_virtual_resource_tools()`、`_virtual_prompt_tools()`。

#### P0-8｜“HITL 到底解决什么？为什么不能只靠 system prompt？”

**面试官为什么问：** 简历声称约束敏感操作，面试官会验证安全边界是否真实。

**先给结论：** 人在回路审批（HITL）给高风险动作增加一次明确授权；system prompt 只是模型行为建议，可能被误解或提示注入影响，不能充当强制安全边界。

**60～120 秒完整口语答案：** Axiom 在非只读等敏感调用前由 Runtime 判断是否需要审批，并把决策放在 Tool 执行之前。理想审批应绑定规范化后的 Tool 名称、参数、调用身份和策略版本；如果批准后参数变化，应重新审批。HITL 仍不能防人误批、Tool 漏洞或宿主权限过大，所以还需要路径/命令校验，生产多租户再加容器、最小权限凭证和网络策略。Prompt 可以解释政策，但不决定最终权限。

**第一轮追问：** “无人值守任务怎么办？”——高风险操作拒绝或进入等待；只允许策略预授权的低风险能力，不能为了自动化绕过审批。

**第二轮追问：** “审批后参数被换了？”——执行前重新计算并校验绑定摘要，不匹配就拒绝或重新审批。

**常见坑：** “用户点了同意就绝对安全”；“system prompt 优先级高所以不会被绕过”。

**项目证据：** `src/axiom/policy/permissions.py`、`ToolExecutor._approval_decision()`。

#### P0-9｜“路径校验和命令校验具体防什么？为什么还不算沙箱？”

**面试官为什么问：** 检查你是否把应用层规则过度包装成强隔离。

**先给结论：** PathGuard 把文件访问限制在规范化工作区，CommandGuard 拦截明显危险命令；它们能提前拒绝常见误操作，但不能覆盖所有 symlink/TOCTOU、Shell 组合和子进程行为，所以不是操作系统沙箱。

**60～120 秒完整口语答案：** 对 `../`，应先把工作区和目标解析成规范化绝对路径，再判断目标是否位于允许根目录，不能做字符串前缀比较。命令侧可拒绝明显删除、破坏或越界模式，并在执行前走审批。但软链接目标可能在检查与使用间变化，Shell 还能通过解释器、脚本和间接命令产生副作用。真正沙箱需要独立文件系统视图、低权限用户、进程/网络/资源限制。当前 Guard 适合本地 defense-in-depth，但必须准确命名。

**第一轮追问：** “符号链接能绕过吗？”——仅一次路径检查不能证明无竞态；需避免跟随链接、使用受控文件描述符或把隔离下沉到 sandbox。

**常见坑：** “用了 `resolve()` 就解决全部路径攻击”；“命令黑名单就是沙箱”。

**项目证据：** `src/axiom/policy/path_guard.py`、`src/axiom/policy/command_guard.py`、policy tests。

### P1 — 回答后的自然深挖（7 题）

#### P1-1｜“Prompt Injection 怎么借 Tool 越权？”

**为什么会被追到这里：** 你说 system prompt 不是安全边界。

**30～60 秒回答：** 网页或仓库内容可能伪装成指令，诱导模型读取凭证或执行 Shell。外部内容只能影响建议，不能提升 Tool 可见性和权限；Runtime 仍按身份、路径、命令、审批和网络规则决定是否执行。

**典型继续追问：** “模型已经把秘密读进上下文呢？” **回答思路：** 最小权限和凭证代理优先，输出/日志再脱敏。**项目边界：** 当前不是完整多租户 prompt-injection 防护平台。

#### P1-2｜“Web 抓取会不会有 SSRF？”

**为什么会被追到这里：** 简历列出了 Web Tool。

**30～60 秒回答：** 会。模型控制 URL 时可能访问环回、云元数据或内网服务；还要防 DNS rebinding 和重定向到私网。生产设计应校验 scheme/host/IP、每次重定向重新判断、限制响应大小和网络出口。

**典型继续追问：** “当前完全防住了吗？” **回答思路：** 诚实说明当前 Web 能力和 guard 有限。**项目边界：** 不声称企业级 SSRF 隔离已实现。

#### P1-3｜“Tool 超时到底意味着没执行吗？”

**为什么会被追到这里：** 你在完整链路中提到超时。

**30～60 秒回答：** 不意味着。可能是排队、连接、服务端处理或响应返回任一阶段超时；写操作可能已经成功但响应丢失。Axiom 先把失败分类，只让 timeout、connection、429 和选定 5xx 进入候选；只读或显式幂等 Tool 才自动重试，普通写 Tool 会记录 `UNKNOWN + unsafe_retry_suppressed` 并把错误交回 Agent。每个 attempt 的有效 timeout 还会被 Run 剩余 deadline 截短。

**典型继续追问：** “幂等键放哪？” **回答思路：** 用稳定业务 operation ID 传给服务端并建立唯一约束。`ToolExecution` 保存同一 invocation 的 attempt、失败类别、下一次重试时间和 exhausted 状态，恢复不会从零计数；但本地记录不能替外部系统实现幂等。

#### P1-4｜“MCP Server 连续失败怎么治理？”

**为什么会被追到这里：** 你说 server 失败不能拖垮 Runtime。

**30～60 秒回答：** 当前先做单次 timeout、外层 Run deadline、失败分类和有界指数退避 full jitter；如果 Run 只剩 2 秒而下次应等 4 秒，就直接停止 retry。取消发生在 backoff 或依赖等待中时，`CancelledError` 保持最高优先级，不会被改写成 exhausted。跨 Run 的连续失败窗口、open/half-open/closed 状态才属于未来 circuit breaker。

**典型继续追问：** “为什么要随机抖动？” **回答思路：** 防止大量 Run 同时重试形成尖峰。**项目边界：** 当前没有通用熔断框架。

#### P1-5｜“一个 MCP Server 暴露几百个 Tool 怎么办？”

**为什么会被追到这里：** 你提到了动态发现。

**30～60 秒回答：** 不能每轮全量塞进 Prompt。先按身份、任务领域和 capability 过滤，再检索少量候选 Schema；执行前仍做 Registry 和 Policy 校验，防止检索结果越权。

**典型继续追问：** “如何评测 Tool 召回？” **回答思路：** 固定任务标注期望 capability，看 Recall@K、误选率和任务成功。**项目边界：** 当前 Registry 面向较小本地工具集。

#### P1-6｜“API Key 和内部凭证放哪？”

**为什么会被追到这里：** 你谈到了远端 HTTP 和多租户安全。

**30～60 秒回答：** 模型和普通 Tool 不应直接看到长期密钥；由受控凭证代理按租户、Tool 和请求注入短期凭证，日志和 Trace 只记录脱敏标识。工作区、进程环境和审计访问也要隔离。

**典型继续追问：** “本地项目现在做到哪？” **回答思路：** 当前有配置屏蔽与审计脱敏基础，但不是企业 secret broker。**项目边界：** 不宣称完整凭证托管。

#### P1-7｜“JSONL 审计和普通日志有什么区别？”

**为什么会被追到这里：** 简历直接写了 JSONL 审计。

**30～60 秒回答：** 普通日志偏诊断，格式和保留可能变化；审计要回答谁在何时以什么授权执行了什么敏感动作及结果，字段更稳定、访问更严格。当前 JSONL 提供本地结构化追踪和有限脱敏，不是不可篡改合规账本。

**典型继续追问：** “哪些内容不该记？” **回答思路：** 密码、Token、原始敏感参数和大 Tool payload。**项目边界：** 还缺集中存储、签名/防篡改、租户身份和保留策略。

### P2 — 攻击、边界与架构取舍（4 题）

#### P2-1｜“既然 Guard 不是沙箱，你这套安全是不是没意义？”

**最安全的结论：** 有意义，但它是纵深防御中的应用层，而不是最终隔离边界。

**回答主线：** Guard 提前拒绝常见越界并给出可解释原因；HITL 增加授权；多租户仍需 OS/container、最小权限和网络控制。

**不要说什么：** “规则能覆盖所有攻击”或反过来“不是沙箱就完全无用”。

**如果继续扩展怎么做：** 每 Run sandbox、只读挂载、受控出口、凭证代理和不可变审批。

#### P2-2｜“Tool 已成功但响应丢了，你凭什么重试？”

**最安全的结论：** 没有幂等或状态查询时不能自动重试写操作。

**回答主线：** timeout 是未知结果；稳定 invocation 和本地账本只能识别逻辑调用，外部服务仍要接受 idempotency key 或返回 operation status。

**不要说什么：** “重试三次总能成功”“SQLite 能保证外部 exactly-once”。

**如果继续扩展怎么做：** capability 级重试契约、幂等键字段、查询/补偿和人工确认状态。

#### P2-3｜“JSONL 能满足企业合规审计吗？”

**最安全的结论：** 当前不能等价。

**回答主线：** 本地 JSONL 缺少集中身份、不可篡改、访问控制、保留/删除政策和跨实例完整性，但对开发复盘已有价值。

**不要说什么：** “有结构化日志就合规。”

**如果继续扩展怎么做：** 发送到受控审计服务，签名/追加写、字段分级、租户授权和生命周期策略。

#### P2-4｜“多租户共享服务里，当前 Tool 安全模型最先缺什么？”

**最安全的结论：** 缺强隔离和租户身份贯穿，而不是再多写几条 Prompt。

**回答主线：** 每 Run 工作区/进程、凭证、网络、CPU/内存和 Tool quota 都要隔离；审批与审计必须绑定 tenant/user。

**不要说什么：** “PathGuard 加 user_id 就够了。”

**如果继续扩展怎么做：** sandbox lifecycle、identity-aware policy、per-tenant secret broker 和集中 audit。

## 10. 重点追问树（6 条）

### 追问树 1：Tool Calling 基础

```text
模型产生 Tool Call 后完整链路是什么？
└─ Tool Schema 能保证参数正确和安全吗？
   └─ Tool 太多时模型怎么选？
      └─ Tool 返回错误后怎么继续？
```

- **“Schema 能保证安全吗？”**
  - **面试官意图：** 检查结构校验与授权是否混淆。
  - **回答思路：** Schema 约束类型/必填/枚举；权限、路径、业务语义和审批由 Runtime 决定。
  - **30～60 秒口述：** “Schema 能在副作用前拒绝缺字段、错类型等结构问题，也让模型更稳定地产生参数；但它不知道用户有没有权限、路径是否越界、金额是否合理。Axiom 仍由 Policy、Guard 和 HITL 在 Executor 内强制判断，Schema 不是安全边界。”
- **“几百个 Tool 怎么选？”**
  - **面试官意图：** 追问 Tool discovery 的规模边界。
  - **回答思路：** 先身份/领域/capability 过滤，再按任务检索少量高质量 Schema；当前没有 Tool Router。
  - **30～60 秒口述：** “不能把几百份 Schema 每轮全塞给模型。合理做法是先按用户权限、任务域和 namespace 做确定性过滤，再检索候选 Tool；名称、描述、参数例子要可区分。最终执行仍回到 Registry/Policy 校验。Axiom 当前面向较小工具集，没有声称已实现通用 Tool Router。”
- **“错误后怎么继续？”**
  - **面试官意图：** 看错误是否会形成无限模型纠错。
  - **回答思路：** 参数错误可反馈，暂态依赖只对安全操作重试，权限/预算保持权威。
  - **30～60 秒口述：** “未知 Tool 或参数错误会变成结构化 ToolResult，让模型在普通预算内修正；timeout、429 等只有经过分类且调用可安全重试时才走有界 retry。权限拒绝、Run Budget 或 deadline 不会被包装成可重试 Tool 错误，无进展也会封顶。”

### 追问树 2：MCP 分层

```text
MCP 和 Function Calling 是什么关系？
└─ JSON-RPC 在哪一层？
   └─ 直接 REST 不行吗？
      └─ tools / resources / prompts 为什么要分？
```

- **“JSON-RPC 在哪一层？”**
  - **面试官意图：** 验证模型接口、应用协议和传输层次。
  - **回答思路：** Function Calling 表达模型意图；MCP 定义能力协议；JSON-RPC 提供请求/响应身份与错误格式。
  - **30～60 秒口述：** “模型先输出函数名和参数，这是 Function Calling。应用根据 Registry 找到 MCP adapter，再通过 MCP SDK 发协议调用；MCP 消息使用 JSON-RPC 风格的 method、params、id、result/error。JSON-RPC 不是 Tool 选择算法，也不等于 HTTP。”
- **“为什么不直接 REST？”**
  - **面试官意图：** 检查是否为了协议而协议。
  - **回答思路：** 单个稳定 API 直接包装最简单；动态发现和多来源统一契约时 MCP 有价值。
  - **30～60 秒口述：** “一个内部 API 完全可以直接封装成 Tool。MCP 的价值是多个服务用统一方式发现和调用 tools/resources/prompts，减少定制适配；它不会自动提供鉴权、限流或幂等。选型看集成规模，不需要把所有 REST 重写成 MCP。”
- **“三种 capability 为什么分？”**
  - **面试官意图：** 看你是否理解读取内容与执行动作的风险差异。
  - **回答思路：** Tool 是动作，resource 是内容，prompt 是模板；统一适配不等于语义相同。
  - **30～60 秒口述：** “Tool 可能有副作用，resource 主要提供可读取内容，prompt 是服务端模板。Axiom 为复用执行链把 resource/prompt 做了虚拟 Tool 入口，但权限和展示仍应看原始类型；当前 rich content 主要文本化，也要承认结构信息可能损失。”

### 追问树 3：Transport

```text
stdio 和 Streamable HTTP 怎么选？
└─ stdio 子进程挂了怎么办？
   └─ Streamable HTTP 就是 SSE 吗？
      └─ 为什么远端连接还要复用？
```

- **“stdio 子进程挂了怎么办？”**
  - **面试官意图：** 从协议追到进程与故障隔离。
  - **回答思路：** 握手/调用失败局部化、结构化错误、deadline；避免无限拉起。
  - **30～60 秒口述：** “stdio 由客户端管理本地 server 进程，通过 stdin/stdout 传协议，日志应走 stderr。启动失败、进程退出或读取超时只应让该 server 的调用失败，并受 Run deadline 和预算限制；不能无限重启形成风暴。当前 Axiom 依赖 SDK 管会话，不是通用进程编排器。”
- **“Streamable HTTP 是 SSE 吗？”**
  - **面试官意图：** 检查 HTTP 流式概念是否混乱。
  - **回答思路：** 它是 MCP 在 HTTP 上的请求/流式响应 transport，不等同传统单向 SSE，也不是 WebSocket。
  - **30～60 秒口述：** “Streamable HTTP 描述 MCP 消息如何通过 HTTP 请求和流式响应传递；实现可能利用流式机制，但不能直接说成传统 EventSource SSE，更不是双向 WebSocket。stdio 与它的核心区别是部署和连接边界。”
- **“为什么连接复用？”**
  - **面试官意图：** 追到 TCP/TLS 与容量基础。
  - **回答思路：** 减少握手延迟和端口压力，同时限制连接池，避免压垮 server。
  - **30～60 秒口述：** “每次调用新建 TCP/TLS 会增加握手延迟和临时端口压力，复用会话/连接更合适。但池不是越大越好，还要有失效重建、并发安全和服务端容量上限。Axiom 当前的会话能力不能外推成完善连接池平台。”

### 追问树 4：HITL 到强隔离

```text
为什么需要 HITL，system prompt 不够吗？
└─ 人也可能误批，HITL 还有什么意义？
   └─ 审批后参数被替换怎么办？
      └─ Prompt Injection 已经诱导模型时谁兜底？
```

- **“人会误批怎么办？”**
  - **面试官意图：** 防止把 HITL 神化成绝对安全。
  - **回答思路：** HITL 增加明确授权与可审计停顿，是纵深防御的一层。
  - **30～60 秒口述：** “HITL 不能消灭误批，但能把高风险动作从模型自动决定升级为人明确授权，并留下上下文。界面应展示动作、目标和影响，危险默认拒绝；底层仍要 Path/Command Guard、最小权限和隔离，不能把责任全推给用户。”
- **“参数被换怎么办？”**
  - **面试官意图：** 检查 TOCTOU 与授权绑定。
  - **回答思路：** 审批绑定规范化 Tool、参数摘要、invocation 和策略版本；变化即重审。
  - **30～60 秒口述：** “批准的不是‘允许 write_file 一次’这种模糊许可，而应绑定规范化后的 Tool 名、参数、稳定调用 ID 和策略版本。执行前重新比对，任何参数变化都拒绝或重新审批，避免审批 A、执行 B。”
- **“Prompt Injection 谁兜底？”**
  - **面试官意图：** 从模型安全追到 capability security。
  - **回答思路：** 不可信文本只能影响建议，不能提升 Tool 可见性、凭证或权限。
  - **30～60 秒口述：** “网页和代码里的指令都按数据处理。即使模型被诱导，Executor 仍独立检查身份、路径、命令、审批和网络政策；密钥也不应直接进入模型 Context。当前 Guard 是本地纵深防御，不是多租户安全沙箱。”

### 追问树 5：路径安全

```text
PathGuard 怎么拦 `../`？
└─ 字符串前缀判断为什么不行？
   └─ symlink / TOCTOU 还能绕吗？
      └─ 真正的 sandbox 需要什么？
```

- **“字符串前缀为什么不行？”**
  - **面试官意图：** 检查路径规范化基本功。
  - **回答思路：** `root2` 前缀、相对段、大小写/分隔符；解析后判断祖先关系。
  - **30～60 秒口述：** “`C:\work2` 也可能以 `C:\work` 开头，`..` 和不同分隔符也会误导字符串比较。应先把工作区和目标规范化成绝对路径，再判断目标是否位于允许根目录；错误时 fail closed，而不是自动修正到另一路径。”
- **“symlink/TOCTOU 呢？”**
  - **面试官意图：** 追问应用层校验极限。
  - **回答思路：** 检查和使用之间目标可变化；一次 `resolve` 不能给强隔离保证。
  - **30～60 秒口述：** “软链接可指向工作区外，检查后目标还可能被替换，这就是 TOCTOU。更强实现要避免跟随链接、基于受控目录文件描述符操作，或者把文件系统视图下沉到容器/沙箱。当前 PathGuard 不声称消灭所有竞态。”
- **“真正 sandbox 是什么？”**
  - **面试官意图：** 看你能否区分规则与 OS 隔离。
  - **回答思路：** 独立文件系统、低权限身份、进程/网络/资源边界。
  - **30～60 秒口述：** “sandbox 要让越权即使发生也没有系统权限完成：受控挂载、低权限用户、进程和网络隔离、CPU/内存限制、短期凭证。Guard 仍有价值，因为它更早、可解释地拒绝常见错误，但它只是其中一层。”

### 追问树 6：超时与幂等

```text
Tool / LLM 超时以后怎么处理？
└─ 429 和 400 都重试吗？
   └─ 为什么指数退避后还要 jitter？
      ├─ Run 只剩 2s，但 backoff 要 4s？
      └─ 写 Tool timeout、有 idempotency key 就 exactly-once？
```

- **“429 和 400 都重试吗？”**
  - **面试官意图：** 检查失败分类是否确定、可解释。
  - **回答思路：** timeout/connection/429/selected 5xx 可候选；validation/auth/policy/permanent 不重试。
  - **30～60 秒口述：** “不会。429 表示暂时受限，若操作本身安全可结合 Retry-After 有界重试；普通 400 多半是参数或配置问题，重发同请求没有意义。认证失败、策略拒绝、Context 太大也不应重试。先分类，再结合操作安全性决定。”
- **“为什么还要 jitter？”**
  - **面试官意图：** 从单请求追到 retry storm。
  - **回答思路：** 指数退避控制频率，随机抖动打散同步客户端；Axiom 用有上限 full jitter。
  - **30～60 秒口述：** “如果一批 Run 同时收到 429，纯指数退避会让它们在 0.5、1、2 秒再次同时撞击依赖。jitter 在上限内随机分散请求，降低尖峰；上限又防延迟无限增长。测试通过注入随机源保持确定性。”
- **“只剩 2 秒怎么办？”**
  - **面试官意图：** 检查 deadline 是否高于 retry policy。
  - **回答思路：** timeout 是一次 attempt 上限，deadline 是外层 lifetime；睡眠/attempt 放不下就停止。
  - **30～60 秒口述：** “不会睡 4 秒。每次 attempt 的 timeout 取配置值和剩余 Run lifetime 的较小值；重试前再检查 backoff 能否放下。外层 wall deadline 权威，不能把 budget error 改名为 retry exhausted。”
- **“写 timeout 和幂等键呢？”**
  - **面试官意图：** 检查未知副作用与 exactly-once 夸大。
  - **回答思路：** timeout 表示结果未知；普通写自动抑制，记录 UNKNOWN；幂等需服务端契约。
  - **30～60 秒口述：** “创建工单可能已成功只是响应丢了，所以普通写不盲重试，ToolExecution 记录 UNKNOWN。若服务端接受稳定 idempotency key 并有唯一约束，重复可映射同一结果，但这仍不是网络层 exactly-once；对方不支持时只能查 operation status、人工确认或补偿。每个可见 retry attempt 都继续计入 Run Budget，crash 后也从持久 attempt 数继续。”

## 11. 面试官攻击面与防守口径

1. **“MCP 是不是套壳 REST？”** 从发现、内容类型和统一契约解释，不贬低 REST。
2. **“你自己实现了 MCP？”** 明确客户端依赖官方 SDK，server 是简化实现。
3. **“HITL 就安全吗？”** 主动说误批、参数绑定和底层隔离。
4. **“PathGuard 是 sandbox？”** 明确不是，说明 symlink/TOCTOU 边界。
5. **“超时统一重试？”** 明确读写和幂等契约。
6. **“Web Tool 会不会打内网？”** 主动承认 SSRF 风险与当前边界。
7. **“JSONL 就合规？”** 区分本地追踪和企业审计。
8. **“几百个 Tool 全传模型？”** 候选召回，但执行权限仍独立校验。

## 12. 当前边界

### 【当前已实现】

统一 Tool 抽象、Registry/Executor、本地内置 Tool、MCP stdio 与 Streamable HTTP 客户端、tools/resources/prompts 适配、分层 timeout/deadline、暂态错误分类、安全重试、指数 full jitter、HITL、路径/命令校验、JSONL 审计，以及 ToolExecution 持久化、retry state 和成功结果复用。

### 【当前部分支持】

MCP rich content 主要归一化为文本；server 是简化实现；安全策略适合本地工作区而非强多租户；Web 能力不是通用浏览器自动化；审计是本地文件而非防篡改平台。

### 【未来可扩展】

每 Run 容器/沙箱、租户身份与凭证代理、网络出口控制、不可变审批、集中审计、Tool 候选召回、依赖级熔断、MCP 会话池和富内容类型保留。

## 13. 面试前 5 分钟速背

### 5 个概念

Registry/Executor 分层；MCP 与 Function Calling 分层；HITL；应用层 guard；Tool 幂等。

### 5 个源码锚点

`tools/base.py`；`tools/executor.py`；`mcp/client.py`；`policy/path_guard.py`；`runtime/checkpoints.py`。

### 必须不思考就能回答的 5 道 P0

为什么分层？两种 MCP transport 如何选？Guard 是沙箱吗？写 Tool 为什么不能无脑重试？提示注入如何约束？

### 3 个陷阱

不要声称完整 MCP server；不要把 HITL 当安全边界；不要把超时等同于未执行。

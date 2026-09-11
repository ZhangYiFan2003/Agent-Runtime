# Agentic RL Bridge v1

Axiom remains an Agent Runtime. The RL bridge converts evidence the Runtime already persists into
validated trajectories and deterministic rewards, then hands those records to an external trainer.
It does not implement PPO, GRPO, a reward model, or a second execution engine.

```text
Axiom Runtime
  -> Checkpoint + ToolExecution + Trace/Span + CompletionVerification + RunMetrics
  -> TrajectoryBuilder
  -> AgentTrajectory
  -> RewardPipeline
  -> RolloutDataset JSONL
  -> trainer-agnostic boundary
       -> AgentLightningAdapter (v1.0.1 compatibility smoke)
       -> TRL environment integration (actual GRPO experiment)
```

## Readiness audit

| Axiom evidence | Agentic RL meaning | v1 interpretation |
| --- | --- | --- |
| durable Run | episode | one trajectory per Run |
| Child Run | sub-episode | separate identity; parent/child IDs are retained |
| model-facing message projection | observation | messages before an assistant decision |
| assistant response/tool calls | action | the decision actually emitted by the policy |
| ToolExecution plus tool message | transition and next evidence | selected tool, arguments, status, attempts, result |
| Checkpoint | environment state | durable truth used to reconstruct, not itself an observation |
| completion verification | outcome verifier | strongest deterministic success evidence |
| evaluation scorers | task reward evidence | reused only after explicit rollout execution |
| tokens, cost, steps, retries | efficiency signals | opt-in penalties; zero by default |
| Trace/Span | raw trajectory evidence | model identity, call identity, timing, usage, status |
| repeated evaluation trial | rollout attempt | independent Run/Thread/Turn identity |

Before v1, the evidence existed but the following abstractions did not: an episode schema,
observation/action transitions, termination normalization, decomposed reward, export validation,
JSONL rollout collection, and a trainer adapter.

There is one fidelity limit. Checkpoints persist the full conversation, while context compaction
spans persist counts and fingerprints rather than the exact compacted message payload. A
non-compacted LLM step is marked `observation.exact=true`; a compacted/projected step is marked
false. The Agent Lightning adapter rejects lossy observations by default. This avoids claiming
byte-for-byte on-policy replay from evidence that cannot support it.

## Trajectory schema and evidence

`AgentTrajectory` contains Run/Trace/Thread/Turn identity, parent and child Run lineage, strategy,
ordered `TrajectoryStep` records, terminal `TrajectoryOutcome`, aggregate metrics, reward, and
provenance. Each step contains:

- the prior model-visible message sequence, its fingerprint, source, and fidelity flag;
- the assistant output and actual tool calls;
- matching tool observations and durable execution status;
- model and LLM span ID, token counts, allocated known cost, timestamp, and latency;
- `done`, terminal reason, and terminal reward components.

The builder uses the final Checkpoint only as durable source material. It does not copy arbitrary
strategy state or an entire checkpoint into training data. Tool arguments and observations are
kept because they are essential for tool-use learning. Known credential keys, bearer values,
private keys, and common API-key forms are redacted before validation and export. This is a
defence-in-depth boundary, not a replacement for dataset review.

Export validation rejects non-terminal Runs, Run/Trace mismatches, LLM/action count mismatches,
orphan tool observations, unordered or duplicate step indexes, non-finite rewards, inconsistent
reward sums, and detectable unredacted secret forms.

Termination is normalized independently from success:

```text
VERIFIED_COMPLETION | UNVERIFIED_COMPLETION | FAILED | NO_PROGRESS
BUDGET_EXCEEDED | DEADLINE_EXCEEDED | CANCELLED | DEPENDENCY_FAILURE
```

A failed or cancelled Run is a completed episode (`done=true`) but not a successful one.

## Reward pipeline

`RewardConfig` is data: every weight is configurable and its canonical representation is hashed.
Defaults are conservative: verified completion or a passed evaluation receives `1.0`; ordinary
unverified completion, tool/process reward, efficiency penalties, and failure penalties default to
zero. Experiments must choose and record any other weights.

The stored decomposition is:

```text
outcome_reward
+ tool_reward
+ step_penalty
+ token_penalty
+ cost_penalty
+ retry_penalty
+ failure_penalty
= total_reward
```

Completion verification takes precedence over weaker completion evidence. Evaluation pass can be
an outcome source when the explicit task scorers pass. Tool reward counts unique successful tool
names rather than calls, can reward a passed `tool_usage` scorer, and is capped. This blocks the
most obvious repeat-an-easy-tool exploit. Efficiency and failure terms are opt-in penalty
magnitudes. Outcome-only reward is the v1 baseline.

These controls do not solve reward hacking. Dataset authors must still check that contracts prove
the actual task, ensure a keyword is not accepted as an artifact, avoid punishing necessary tools,
and inspect component distributions. Badcases are candidates for reviewed dataset work, never
automatic training examples.

## Evaluation versus rollout

Evaluation compares versions and detects regressions. An RL rollout collects policy interactions
and reward for training. `RLRolloutRunner` deliberately reuses `EvaluationRunner` and
`DurableEvaluationExecutor`, but creates a separate `RolloutDataset` lifecycle. Each repeated trial
is an independent root episode. Child Runs are emitted as separate sub-episodes with lineage rather
than flattened into their parent; v1 assigns no custom multi-agent credit.

The v1 environment is deliberately narrow: deterministic repository/coding-agent tasks such as
symbol lookup, caller/callee tracing, constrained edits, tool selection, and test-backed
completion. The maintained `benchmarks/datasets/agent-core.json` has 12 repository-grounded cases,
which is enough to validate plumbing but not to support a performance claim.

The CLI executes a reviewed evaluation dataset through the durable Runtime and writes one episode
per JSONL record:

```powershell
axiom rl export benchmarks/datasets/agent-core.json `
  --trials 2 `
  --split train `
  --reward-config reward.json `
  --output artifacts/agent-core-train.jsonl
```

For example, an explicitly cost-aware `reward.json` can be:

```json
{
  "verified_outcome_reward": 1.0,
  "evaluation_pass_reward": 1.0,
  "unverified_completion_penalty": 0.25,
  "failed_penalty": 0.5,
  "step_penalty": 0.005,
  "token_penalty_per_1k": 0.001,
  "successful_tool_reward": 0.0,
  "max_tool_reward": 0.0
}
```

These are example experiment choices, not claimed scientific defaults. The default remains
outcome-only.

The summary reports rollout count, valid/invalid count, mean reward, component means, mean episode
length, and verified-completion rate. Malformed evidence fails the export; it is not skipped.

Provenance retains safe runtime/model/config/prompt/tool-schema fingerprints from evaluation,
reward-config fingerprint, dataset/version/task/trial, split, held-out flag, evidence schema
versions, and objective fingerprint. Raw API keys and raw local configuration are excluded.

## Trainer compatibility and selection

The comparison and compatibility audit were refreshed on 2026-09-11. Agent Lightning 1.0.1 is the
selected compatibility target. It no longer exposes a public `Triplet` constructor. Axiom now emits
the public `EventCreate` boundary (`model_request` and terminal `reward`); Agent Lightning's rollout
manager owns internal Triplet construction. Exact server-captured prompt/response IDs are required.
The adapter rejects retokenized IDs, lossy observations, and multi-call prefix discontinuities.

A native-object smoke test passed against 1.0.1. A two-call trajectory separated by two tool
observation tokens aggregated to response IDs `[20, 30, 31, 21]` with response mask
`[1, 0, 0, 1]`, so only the two policy tokens are trainable.

Agent Lightning/veRL was not selected for the laptop experiment: its local controller is
POSIX-only, WSL was not installed, Docker had no active daemon, and its distributed stack was a
poor fit for 8 GiB VRAM and 15.64 GiB RAM. TRL 1.13.0's supported `environment_factory` boundary
was used instead. Axiom owns tool execution, verification, trajectory evidence, and reward; TRL
owns generation, GRPO loss/advantage, optimization, and checkpointing.

Primary references:

- [Agent Lightning v1 documentation](https://microsoft.github.io/agent-lightning/latest/)
- [Agent Lightning trace adapters](https://github.com/microsoft/agent-lightning/blob/main/docs/tutorials/traces.md)
- [veRL Agent Loop](https://verl.readthedocs.io/en/latest/advance/agent_loop.html)
- [veRL repository](https://github.com/verl-project/verl)
- [TRL GRPO agent training](https://huggingface.co/docs/trl/grpo_trainer)

## Measured experiment

The first manual experiment is recorded in `experiments/agentic-rl-v1/`. It used deterministic
repository-navigation tasks (42 train, 12 dev, 15 held-out), Qwen3-0.6B, TRL 1.13.0 GRPO, and LoRA
on an RTX 4060 Laptop GPU. A 42-step update completed, its 2,293,760 trainable parameters changed
fingerprint, loss and gradients were finite, and the saved adapter reloaded successfully.

The task result was negative: base and trained policies both achieved **0/30 held-out successes**
and **0/30 VERIFIED completions** over two trials per task. Mean shaped reward changed from -0.9710
to -0.9617 and output tokens from 52.30 to 28.90, but outcome-only reward stayed -1.0. Inspection
showed shorter and sometimes more useful searches, never the required read-plus-submit sequence.
This is process shaping, not task improvement.

The leakage audit found disjoint task/feature labels but shared repository snapshots whose files
span all splits. That limits generalization claims even though the measured success delta was zero.
See the experiment README and lightweight JSON evidence for the full A-L completion report.

## Final maturity

| Capability | Status |
| --- | --- |
| RL-ready Runtime evidence | Complete |
| Trajectory construction | Complete |
| Reward pipeline | Complete |
| Rollout collection | Complete |
| Agent Lightning v1 compatibility | Smoke-tested |
| External trainer boundary | Complete |
| Real GRPO/LoRA training | Complete |
| Checkpoint reload/update proof | Complete |
| Held-out capability improvement | **Not achieved** |
| Large-scale Agentic RL | Not implemented |

Engineering loop success and model capability gain are separate claims. The first GRPO experiment
successfully closed the engineering training loop, but produced no held-out capability improvement.

## Why the negative result matters

1. **Zero-success exploration region.** Base held-out evaluation was 0/30, and all 168 training
   rollouts also failed. The available group-relative reward signal contained no demonstrated
   successful `search -> inspect -> read -> synthesize -> submit` behavior, making useful credit
   assignment extremely weak. This does not mean GRPO mathematically cannot learn from zero success.
2. **Sparse outcome reward.** Deterministic terminal verification was the strongest reward. The
   weak 0.6B policy rarely reached the complete evidence-and-submission path needed to receive it.
3. **Reward shaping shortcut.** A useful first search followed by stopping could score slightly
   better than continuing and still failing. The trained policy therefore learned the proxy “fail
   more cheaply,” not “solve the task.” This is reward hacking-like proxy optimization, not a claim
   of catastrophic reward hacking.
4. **Base-model capability.** Qwen3-0.6B may be below the capability threshold for this multi-step
   navigation task. The zero-success baseline supports that hypothesis but does not prove a general
   model-size threshold.
5. **Dataset isolation.** Questions and feature labels were disjoint, but repositories exposed
   files from all splits. The result does not prove strict repository-level generalization.

## Experiment-linked fundamentals

- **Policy:** the base or LoRA-updated model plus sampling configuration that chose tools and text.
- **Environment:** Axiom's Runtime, tools, permissions, workspace, and deterministic verifier.
- **Episode:** one terminal durable Run; a Child Run is a linked sub-episode.
- **Observation:** what the policy sees before a model call, distinct from durable state.
- **Action:** emitted assistant content, tool selection/arguments, or final response.
- **Trajectory:** the ordered observation/action/tool-observation transitions for an episode.
- **Rollout:** one policy attempt on one task; this experiment trained on 168 independent attempts.
- **Reward:** deterministic numeric feedback for a rollout. **Return** is accumulated reward over an
  episode; v1 primarily used a terminal outcome plus small process/efficiency components.
- **Outcome reward:** terminal success evidence, preferably completion verification.
- **Sparse versus process reward:** VERIFIED was sparse terminal reward; a useful search was denser
  process reward. The latter created the observed “one useful search, then stop” shortcut.
- **Credit assignment:** deciding which actions deserve the return. With no successful rollout,
  attributing terminal success to search/read/submit decisions was especially weak.
- **On-policy:** collect with the policy currently being updated. The TRL experiment captured exact
  prompt, response, and loss-mask IDs directly from generation; it never trained on reconstructed
  Axiom observations. Agent Lightning's proxy is the corresponding future integration mechanism.
- **Off-policy:** learn from trajectories produced by another/older policy; JSONL may support
  analysis or compatible trainer workflows but is not mislabeled as on-policy.
- **Advantage and GRPO:** advantage measures a sampled action's relative value. GRPO estimated it
  from rewards within each four-completion prompt group instead of using a learned value model.
- **PPO:** an external clipped policy-gradient alternative that typically uses a critic; Axiom did
  not implement or run it.
- **KL regularization:** constrains drift from a reference policy. This tiny run used `beta=0`, so
  no reference-policy KL penalty; that is recorded experiment configuration, not a general choice.
- **Exploration:** the policy must visit useful action sequences before outcome reward can reinforce
  them. Here it never visited a successful full sequence.
- **Reward hacking / proxy optimization:** shaped reward increased while success stayed zero,
  demonstrating why proxy reward cannot replace held-out task success.
- **SFT -> RL:** a future SFT warm-up could demonstrate successful trajectories before RL. It is a
  v2 prerequisite option, not work performed in v1.

## Experiment v2 prerequisites (future work only)

No Experiment v2 has started. A future run requires at least one of: a stronger base policy with
non-zero task success, successful SFT warm-up trajectories, or a curriculum environment with a
learnable intermediate signal. It must use strict repository/feature snapshot isolation. Start
with outcome-only reward, add deterministic process reward only when evidence justifies it, and
apply RL only after the policy enters a solvable region. Merely tuning the current learning rate,
reward weights, group size, or epoch count is not an acceptable next step.

## Feature freeze

**Agentic RL v1 is now frozen.** Further training must not consist only of hyperparameter tuning on
the current zero-success setup. Any future work belongs to the explicitly scoped Experiment v2
prerequisites above.

## v1 limits

The bridge has unit-tested plumbing and a real negative training result, not evidence that RL
improves task performance. It has no semantic
judge, reward-model training, per-token reward, sophisticated multi-agent credit assignment,
generic open-ended environment, distributed rollout cluster, custom PPO/GRPO, or large rollout
dataset. Exact compacted prompt replay remains unavailable until the Runtime persists a safe
model-request projection or uses trainer-side live tracing.

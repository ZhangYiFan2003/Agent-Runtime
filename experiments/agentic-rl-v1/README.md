# Agentic RL Experiment v1

## Outcome

This experiment completed a real GRPO/LoRA update and a frozen before/after evaluation. It did
**not** improve task success: both policies solved **0/30 held-out rollouts (0/15 unique tasks,
two trials each)**. The trained policy made shorter, sometimes more useful first searches, so its
shaped reward rose from -0.9710 to -0.9617, but outcome-only reward remained -1.0. This is a
negative task-learning result, not measured RL improvement.

```text
Engineering loop success   YES
Model capability gain      NO
```

The leakage audit also found that the train/dev/held-out feature IDs are disjoint while the same
three repository snapshots expose files from every split. A training rollout could inspect a
held-out feature file incidentally. Since there was no success improvement this does not create a
false positive, but it prevents a strong generalization claim.

## A. Compatibility audit

- Current stable Agent Lightning checked and installed in an isolated environment: **1.0.1**.
- The old Axiom adapter was incompatible: v1.0.1 has no public `agentlightning.Triplet` API. The
  integration now emits public `agentlightning.schemas.EventCreate` records (`model_request`, then
  terminal `reward`) and leaves internal Triplet construction to Agent Lightning.
- Exact server-captured `prompt_token_ids`, `response_token_ids`, and optional log probabilities
  are required. Retokenized IDs and lossy Axiom observations are rejected.
- The native-object smoke test passed. For two model calls separated by tool observation tokens,
  the aggregation audit produced response IDs `[20, 30, 31, 21]` and loss mask `[1, 0, 0, 1]`:
  policy responses are trainable, tool/environment observations are context only.
- Agent Lightning's recommended trajectory aggregation joins the next prompt only when it is an
  exact prefix extension of the previous prompt plus response. Axiom rejects a prefix mismatch
  instead of allowing a rollout to split silently.
- Exact experiment stack: Agent Lightning 1.0.1 (audit only), TRL 1.13.0, PyTorch 2.14.0+cu130,
  Transformers 5.17.0, PEFT 0.20.0, CUDA runtime 13.0. Host: Windows 11; WSL/Linux was not
  installed. Hardware was an RTX 4060 Laptop GPU with 8,188 MiB VRAM and 15.64 GiB system RAM.

Evidence: `results/agent_lightning_compatibility.json` and `config/versions.json`.

For the TRL run, `results/on_policy_audit.json` checks all 168 training rollouts: exact IDs and
masks came directly from TRL's generation-and-score output, rewards matched their Axiom
trajectories, and 1,065 tool-observation tokens were masked from loss. The reconstructed Axiom
observation projection remains explicitly `exact=false` and was not used as training input.

## B. Trainer decision

Agent Lightning/veRL was not used for optimization. Its local controller is POSIX-only, WSL was
absent, Docker had no running daemon, and adding its distributed veRL/Ray path to this 8 GiB laptop
would have been disproportionate. TRL 1.13.0 was selected because its supported
`environment_factory` path performs multi-turn tool rollouts and environment-owned rewards on one
GPU. Axiom remained the environment: its Tool registry/executor, deterministic verifier,
trajectory model, and RewardPipeline were reused. TRL owned generation, group normalization,
GRPO loss, optimizer updates, and checkpointing.

## C. Dataset

The domain is deterministic repository navigation: locate a class definition, trace a retry key
to config, or locate a happy-path test. Every task requires search, reading the submitted evidence
file, and an exact file/symbol submission. Counts are **42 train / 12 dev / 15 held-out** across
three fixtures. Dataset fingerprint:

`sha256:ab1eb876518e94834d6b1e0ded64d26e46c02760b4b1466439e8d8e10d2487d6`

Task IDs, features, and exact questions do not overlap across splits; completion contracts and tool
metadata do not reveal the expected answer. Strict repository-snapshot isolation failed for the
reason in the Outcome section. Full audit: `results/leakage_audit.json`.

## D. Baseline

The frozen base model was evaluated before any nonzero-gradient training. On held-out it achieved
0/30 success, 0/30 VERIFIED, -0.9710 mean shaped reward, -1.0 outcome-only reward, 1.50 mean agent
steps, 1.20 tool attempts, 549.67 input tokens, 52.30 output tokens, and 30/30 NO_PROGRESS. All
failures were `no_submission`. Dev was also 0/24.

The committed baseline artifact is `results/baseline_metrics.json`. Raw dev/held-out trajectory
JSONL remains local and ignored because it is substantially larger than the aggregate evidence.
The official top-level summary contains the corrected deterministic reward rescore; embedded
`trainer_*_metrics` retain the pre-rescore generation-time reward as raw trainer telemetry.

## E. Training

- Model: Qwen/Qwen3-0.6B; GRPO with LoRA r=8, alpha=16 on q/k/v/o projections.
- BF16, gradient checkpointing, batch 1, accumulation 4, group size 4, temperature 1.0,
  top-p 0.9, maximum 384 completion tokens, 4 tool-call iterations.
- Group rewards were normalized within each four-completion prompt group (`scale_rewards=group`),
  `loss_type=dapo`, beta 0 (no reference policy), learning rate 5e-6.
- One epoch: 42 optimizer steps and 168 rollouts. Mean train reward -0.96726; success 0/168.
- Peak CUDA allocation 2,027.4 MiB; peak reservation 2,996.0 MiB; runtime 180.55 seconds.

Outcome dominated reward. `outcome_only` used +/-1 terminal outcomes. The trained configuration
added +0.05 per distinct useful tool family (cap +0.15) and -0.005 per step. It did not reward
non-crashing but empty searches.

## F. Proof of update

The three-step smoke run first verified a real update and reload. The full run had 2,293,760
trainable parameters of 598,343,680 total, 42 optimizer steps, finite loss 0.217001, 9,408 checked
gradient tensors with zero non-finite tensors, and a successful adapter reload. The trainable
fingerprint changed from:

`sha256:33cd6b0200af9cc1d45ccd85104975631d2bb0e1e7ea19f87d2c8f25095a7911`

to:

`sha256:3d7f985dab3ae35b1efede5597e9646f7809b454cc07895e71cb026a18bb6623`

The first attempted smoke produced zero group variance and no parameter change; it was explicitly
rejected. Both that negative control and the successful proof are preserved in
`results/smoke_zero_variance_proof.json` and `results/smoke_proof.json`.

## G. Final held-out results

| Metric | Base | Trained |
| --- | ---: | ---: |
| Success | 0/30 | 0/30 |
| VERIFIED | 0/30 | 0/30 |
| Mean shaped reward | -0.9710 | -0.9617 |
| Mean outcome-only reward | -1.0000 | -1.0000 |
| Mean agent steps | 1.5000 | 1.1333 |
| Mean tool attempts | 1.2000 | 1.0000 |
| Mean tokens (input/output) | 549.67 / 52.30 | 549.67 / 28.90 |
| NO_PROGRESS | 30/30 | 30/30 |

The same 15 tasks, two trials, runtime/tool schema, contracts, temperature, top-p, limits, and seed
were used. Only the base versus LoRA checkpoint changed.

## H. Behavioral examples

- `heldout-beacon-timber-config`, trial 1: base made four increasingly verbose empty searches
  (251 policy tokens); trained searched `TimberService` once and obtained evidence (22 tokens).
  Both still failed to read and submit.
- `heldout-beacon-timber-test`, trial 1: base looped through four empty generic searches (200
  policy tokens); trained made one useful `test` search (20 tokens). Both still failed.
- Regression, `heldout-beacon-umber-test`, trial 1: trained expanded an empty query and used 56
  policy tokens versus 44 for base, with the same -1.005 reward and no submission.
- Reward-hacking audit: neither policy submitted expected strings without evidence, prematurely
  submitted, or avoided all evidence tools on held-out. However, the trained policy often made one
  useful search and stopped: this earns -0.955 instead of -1.005 while still failing. That is a
  reward-shaping shortcut, not task learning, and must not be reported as improvement. The reward
  was not changed and the experiment was not rerun to chase a positive result.

## I. Limitations

This is one 0.6B model, one 8 GiB laptop GPU, 42 tasks, one epoch, sparse terminal success, no
successful exploratory rollout, and only deterministic repository fixtures. Snapshot isolation
failed even though task/symbol splits were disjoint. The experiment says nothing reliable about
larger models, richer repositories, longer training, or general agentic-RL performance.

## Diagnosis

1. **Zero-success exploration region.** The baseline held-out evaluation was 0/30 and all 168
   training rollouts also failed. The available group-relative reward signal contained no
   demonstrated successful `search -> inspect -> read -> synthesize -> submit` behavior, making
   useful credit assignment extremely weak. This is not a claim that GRPO can never learn from a
   zero-success batch.
2. **Sparse outcome reward.** Deterministic terminal verification was the strongest signal, but
   this 0.6B policy almost never reached the full verification path.
3. **Reward shaping shortcut.** One useful search followed by early stopping scored slightly
   better than continued unsuccessful action. The optimizer learned to fail more cheaply instead
   of solving the task. This is proxy optimization or reward hacking-like behavior, not
   catastrophic reward hacking.
4. **Base-model capability.** Qwen3-0.6B may be below the capability threshold for reliable
   multi-step repository navigation. The zero-success baseline supports this hypothesis but does
   not prove a universal model-size threshold.
5. **Dataset isolation.** Questions and feature labels were separated, but repository snapshots
   still contained held-out files, so this experiment does not establish repository-level
   generalization.

## Experiment v2 prerequisites (future work only)

No v2 work is implemented. A future experiment requires a stronger base policy with non-zero task
success, successful SFT warm-up trajectories, or a curriculum that creates learnable intermediate
behavior. It must also use strict repository/feature snapshot isolation. Evaluation should start
with outcome-only reward, add deterministic process reward only when justified, and begin RL only
after the policy enters a meaningfully solvable region. More epochs or hyperparameter fishing on
the current zero-success setup is not the plan.

## J. Files changed

Changes are restricted to the RL adapter/tests, this experiment's dataset/environment/runner,
lightweight aggregate evidence, and documentation. Checkpoints, model weights, caches, and all raw
trajectory dumps are ignored.

## K. Tests / CI

Focused RL bridge and experiment tests, dataset regeneration checks, the Agent Lightning 1.0.1
native-object smoke, Ruff, compileall, and `git diff --check` are the local gates. Full GitHub CI is
left to run only after review/publish; heavy model download and GRPO are not part of normal CI.

## L. Resume / interview recommendation

Keep this as an honest interview/project extension, not a quantified resume improvement claim.
It proves a trainer-agnostic boundary and a real loadable policy update, while the measured task
result was flat. A defensible summary is: “Connected Axiom's deterministic tool environment to
TRL GRPO, completed and fingerprint-verified a LoRA update on a 0.6B model, then measured 0/30 to
0/30 held-out success; analysis showed shorter first searches but no learned completion.”

## Reproduction

The experiment is manual and uses disposable environments under `.tmp`; it never installs the RL
stack into Axiom's normal Windows environment.

```powershell
.\.venv\Scripts\python.exe experiments\agentic-rl-v1\prepare_dataset.py --check
$env:PYTHONPATH = "$PWD\src;$PWD\.tmp\agl-compat\Lib\site-packages"
.\.venv\Scripts\python.exe experiments\agentic-rl-v1\smoke_agent_lightning.py
.\.tmp\trl-v1\Scripts\python.exe experiments\agentic-rl-v1\run_experiment.py baseline
.\.tmp\trl-v1\Scripts\python.exe experiments\agentic-rl-v1\run_experiment.py smoke
.\.tmp\trl-v1\Scripts\python.exe experiments\agentic-rl-v1\run_experiment.py train
.\.tmp\trl-v1\Scripts\python.exe experiments\agentic-rl-v1\run_experiment.py evaluate
.\.venv\Scripts\python.exe experiments\agentic-rl-v1\audit_results.py
```

Baseline must always precede training. Do not regenerate or tune the frozen held-out split after
seeing its result. Agentic RL v1 is frozen; these commands document provenance and are not a request
to rerun the experiment.

# Agent Runtime Fixed-Task Evaluation

`dataset.json` contains 40 repository-grounded, read-only tasks. It uses the
existing deterministic scorers only: run status, contains, exact match, tool
usage, and metric thresholds. No LLM-as-a-judge is used.

Validate without a provider:

```powershell
uv run python -m benchmarks.agent_runtime.run_agent_benchmark --validate-only
```

Run with a normally configured real provider:

```powershell
uv run python -m benchmarks.agent_runtime.run_agent_benchmark `
  --output benchmarks/agent-runtime/results/provider-baseline.json
```

Fake/scripted LLMs may exercise Runtime execution correctness but must not be
reported as Agent Task Success Rate. Provider results must record the exact
provider/model separately and must never contain credentials.

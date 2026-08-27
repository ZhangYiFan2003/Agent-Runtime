# Durable Recovery Fault-Injection Evaluation

The matrix maps maintained deterministic crash/restart tests into 21 named
ReAct, Plan, and Multi-Agent scenarios. Each runs twice by default, producing
42 executions without a model provider or timing races.

```powershell
uv run python -m benchmarks.recovery.run_fault_injection
```

Passing scenarios are classified as recovered or expected-safe. The latter is
used for ambiguous external side effects that correctly stop for explicit
recovery rather than silently re-executing. Duplicate/loss metrics are zero only
where the mapped test explicitly asserts the property.

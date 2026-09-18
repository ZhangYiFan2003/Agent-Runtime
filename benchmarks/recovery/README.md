# Durable Recovery Fault-Injection Evaluation

The matrix maps maintained deterministic crash/restart tests into 25 named
ReAct, Plan, Multi-Agent, and distributed-redelivery scenarios. Each runs twice
by default, producing 50 executions without a model provider or timing races.
The distributed-redelivery cases require `AXIOM_TEST_POSTGRES_DSN` and a real
PostgreSQL database; they are not mock concurrency evidence.

```powershell
uv run python -m benchmarks.recovery.run_fault_injection
```

Passing scenarios are classified as recovered or expected-safe. The latter is
used for ambiguous external side effects that correctly stop for explicit
recovery rather than silently re-executing. Duplicate/loss metrics are zero only
where the mapped test explicitly asserts the property.

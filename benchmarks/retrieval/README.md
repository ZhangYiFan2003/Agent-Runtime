# Code Intelligence Retrieval Evaluation

This benchmark evaluates the checked-in Axiom source tree with 80 maintained,
repository-grounded queries. Ground truth comes from source definitions and
references, not model output.

The dataset has four balanced query types: lexical-oriented,
semantic/paraphrase-oriented, symbol-oriented, and graph/context-oriented.
Categories cover Runtime lifecycle, checkpoints, permissions, Plan and
Multi-Agent scheduling, MCP, memory, observability, evaluation, Code
Intelligence, tools, configuration, and HTTP APIs.

Run the offline baseline from the repository root:

```powershell
uv run python -m benchmarks.retrieval.evaluate_retrieval
```

Ranked retrieval modes are lexical BM25, deterministic vector scan, and hybrid
BM25/vector RRF. The deterministic token/character hash provider is an offline
pipeline fixture, not a production semantic embedding model. A real embedding
run must be reported separately with the exact provider and model.

`Recall@K` means the fraction of queries with at least one relevant target in
the top K. `Relevant Coverage@K` is the mean fraction of all maintained targets
retrieved. `MRR@5` uses the first relevant target and is truncated at rank 5.

Graph-aware context expansion does not produce a comparable ranked retrieval
list. Its case hit rate and relevant-target coverage are therefore reported in
a separate context-coverage section, never as Recall.

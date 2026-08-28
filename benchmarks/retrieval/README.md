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

## Retrieval v2 protocol

The committed 80-query `dataset.json` and `results/retrieval-offline-baseline.*`
remain the immutable v1 analysis baseline. Retrieval v2 adds:

- `development-dataset.json`: 40 queries, ten per query type, used for the
  five-configuration fusion-weight check;
- `holdout-dataset.json`: 20 queries, five per query type, evaluated once only
  after weights and implementation were frozen;
- `analysis/v1-failure-analysis.*`: complete Hybrid v1 Top-5 misses and
  candidate-generation-versus-ranking diagnosis;
- `results/retrieval-v2-development.*`, `retrieval-v2-holdout.*`, and
  `retrieval-v1-v2-comparison.*`: generated evidence artifacts.

Run the ordered workflow from the repository root:

```powershell
uv run python -m benchmarks.retrieval.analyze_v1_failures
uv run python -m benchmarks.retrieval.tune_v2_weights
uv run python -m benchmarks.retrieval.evaluate_retrieval_v2
uv run python -m benchmarks.retrieval.evaluate_retrieval_v2 `
  --dataset benchmarks/retrieval/holdout-dataset.json `
  --output benchmarks/retrieval/results/retrieval-v2-holdout.json `
  --allow-holdout
uv run python -m benchmarks.retrieval.compare_v1_v2
```

`hybrid` remains the reproducible lexical/vector v1 mode. `hybrid_v2` is an
explicit opt-in mode that fuses lexical-v2, deterministic vector, and symbol
candidates with weighted RRF. `auto` retains its v1 compatibility behavior.
Symbol matching normalizes case, camel/PascalCase,
snake_case, qualified identifiers, and path hints without query-specific
aliases. Lexical v2 retains field-weighted FTS5 ranking and adds a general OR
candidate fallback when strict AND matching does not fill the pool.

Candidate Recall@K means at least one relevant target occurs in any source's
Top-K candidates. It diagnoses candidate generation and is not final ranked
Recall@K. Deterministic vector results remain pipeline regression evidence, not
production semantic quality.

# Retrieval v2 Holdout Result

> Deterministic offline evidence; vector quality is not production semantic quality.

- Dataset: `axiom-repository-retrieval-holdout` (20 queries)
- Git commit: `542952444ca2d9c40a865c828481a2bcf1809251`
- V2 weights: `{'lexical_weight': 0.4, 'vector_weight': 0.1, 'symbol_weight': 0.5, 'candidate_limit_per_source': 200, 'weight_selection': 'development split only'}`

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@5 | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical_v1 | 0.3500 | 0.4000 | 0.4500 | 0.3850 | 2.3410 | 5.5087 |
| vector_deterministic | 0.2500 | 0.4000 | 0.5500 | 0.3325 | - | - |
| hybrid_v1 | 0.4000 | 0.5500 | 0.7500 | 0.5117 | 159.0026 | 184.2341 |
| lexical_v2 | 0.5000 | 0.5500 | 0.7000 | 0.5575 | - | - |
| symbol | 0.5500 | 0.7500 | 0.7500 | 0.6417 | - | - |
| hybrid_v2 | 0.7000 | 0.8500 | 0.8500 | 0.7667 | 186.6748 | 205.2859 |

## Hybrid category metrics

| Query type | V1 Recall@5 | V1 MRR@5 | V2 Recall@5 | V2 MRR@5 |
| --- | ---: | ---: | ---: | ---: |
| graph/context-oriented | 0.8000 | 0.4900 | 1.0000 | 1.0000 |
| lexical-oriented | 0.8000 | 0.6667 | 1.0000 | 0.8667 |
| semantic/paraphrase-oriented | 0.4000 | 0.0900 | 0.4000 | 0.3000 |
| symbol-oriented | 1.0000 | 0.8000 | 1.0000 | 0.9000 |

## Candidate recall

- V1 sources @20/@50: 0.7500 / 0.8000
- V2 sources @20/@50: 0.9000 / 0.9000

## Graph context

- Case hit rate: 1.0000
- Relevant target coverage: 0.9000

Real embedding benchmark: not executed.

# Retrieval v2 Analysis Result

> Deterministic offline evidence; vector quality is not production semantic quality.

- Dataset: `axiom-repository-retrieval` (80 queries)
- Git commit: `542952444ca2d9c40a865c828481a2bcf1809251`
- V2 weights: `{'lexical_weight': 0.4, 'vector_weight': 0.1, 'symbol_weight': 0.5, 'candidate_limit_per_source': 200, 'weight_selection': 'development split only'}`

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@5 | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical_v1 | 0.3750 | 0.4375 | 0.4375 | 0.4021 | 2.4042 | 5.2306 |
| vector_deterministic | 0.1375 | 0.3625 | 0.4250 | 0.2435 | - | - |
| hybrid_v1 | 0.4125 | 0.5125 | 0.5125 | 0.4583 | 158.7101 | 194.4150 |
| lexical_v2 | 0.5125 | 0.6875 | 0.7250 | 0.5998 | - | - |
| symbol | 0.5750 | 0.6375 | 0.6750 | 0.6123 | - | - |
| hybrid_v2 | 0.6250 | 0.7500 | 0.7625 | 0.6858 | 204.3882 | 242.8995 |

## Hybrid category metrics

| Query type | V1 Recall@5 | V1 MRR@5 | V2 Recall@5 | V2 MRR@5 |
| --- | ---: | ---: | ---: | ---: |
| graph/context-oriented | 0.2500 | 0.2000 | 1.0000 | 0.8500 |
| lexical-oriented | 0.8500 | 0.7083 | 0.8000 | 0.7750 |
| semantic/paraphrase-oriented | 0.0000 | 0.0000 | 0.2500 | 0.1183 |
| symbol-oriented | 0.9500 | 0.9250 | 1.0000 | 1.0000 |

## Candidate recall

- V1 sources @20/@50: 0.7750 / 0.8000
- V2 sources @20/@50: 0.9375 / 0.9750

## Graph context

- Case hit rate: 1.0000
- Relevant target coverage: 0.6583

Real embedding benchmark: not executed.

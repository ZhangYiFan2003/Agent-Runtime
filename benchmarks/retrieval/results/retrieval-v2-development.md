# Retrieval v2 Development Result

> Deterministic offline evidence; vector quality is not production semantic quality.

- Dataset: `axiom-repository-retrieval-development` (40 queries)
- Git commit: `542952444ca2d9c40a865c828481a2bcf1809251`
- V2 weights: `{'lexical_weight': 0.4, 'vector_weight': 0.1, 'symbol_weight': 0.5, 'candidate_limit_per_source': 200, 'weight_selection': 'development split only'}`

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@5 | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical_v1 | 0.3000 | 0.3750 | 0.4000 | 0.3425 | 2.0317 | 3.4236 |
| vector_deterministic | 0.3250 | 0.5250 | 0.6500 | 0.4467 | - | - |
| hybrid_v1 | 0.3500 | 0.5500 | 0.5750 | 0.4383 | 146.3369 | 172.8171 |
| lexical_v2 | 0.5250 | 0.6750 | 0.7250 | 0.6100 | - | - |
| symbol | 0.7250 | 0.7250 | 0.7500 | 0.7312 | - | - |
| hybrid_v2 | 0.6750 | 0.7750 | 0.7750 | 0.7250 | 197.8937 | 256.8444 |

## Hybrid category metrics

| Query type | V1 Recall@5 | V1 MRR@5 | V2 Recall@5 | V2 MRR@5 |
| --- | ---: | ---: | ---: | ---: |
| graph/context-oriented | 0.5000 | 0.2667 | 1.0000 | 0.9500 |
| lexical-oriented | 1.0000 | 0.8533 | 1.0000 | 1.0000 |
| semantic/paraphrase-oriented | 0.1000 | 0.1000 | 0.1000 | 0.1000 |
| symbol-oriented | 0.7000 | 0.5333 | 1.0000 | 0.8500 |

## Candidate recall

- V1 sources @20/@50: 0.8500 / 0.9000
- V2 sources @20/@50: 0.9500 / 1.0000

## Graph context

- Case hit rate: 1.0000
- Relevant target coverage: 0.9500

Real embedding benchmark: not executed.

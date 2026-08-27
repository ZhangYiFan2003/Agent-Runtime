# Repository Retrieval Offline Baseline

> Deterministic offline evidence. The hash embedding is not a semantic model.

- Git commit: `2516518260894f350aa4e3df9ed937ff2d382d0f`
- Timestamp: 2026-08-26T02:40:56.046967+00:00
- Python: 3.12.13
- Queries: 80

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@5 | Relevant Coverage@5 |
| --- | ---: | ---: | ---: | ---: | ---: |
| lexical | 0.3750 | 0.4375 | 0.4625 | 0.4077 | 0.4625 |
| vector | 0.1375 | 0.3625 | 0.4250 | 0.2435 | 0.3604 |
| hybrid | 0.4250 | 0.5125 | 0.5125 | 0.4646 | 0.4833 |

## Graph-aware bounded context

These are context coverage metrics, not ranked retrieval Recall.

- Case hit rate: 0.7000
- Relevant target coverage: 0.4417

## Boundary

Real embedding benchmark: not executed.

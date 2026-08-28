# Retrieval v2 Development Weight Selection

> Development split only. Holdout was not evaluated.

- Cases: 40
- Selection rule: maximize dev Recall@5, then MRR@5, then Recall@1

| Lexical | Vector | Symbol | Recall@1 | Recall@3 | Recall@5 | MRR@5 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.40 | 0.20 | 0.40 | 0.6500 | 0.7750 | 0.7750 | 0.7125 |
| 0.30 | 0.20 | 0.50 | 0.6000 | 0.7750 | 0.7750 | 0.6875 |
| 0.40 | 0.10 | 0.50 | 0.6750 | 0.7750 | 0.7750 | 0.7250 |
| 0.50 | 0.10 | 0.40 | 0.6750 | 0.7750 | 0.7750 | 0.7250 |
| 0.35 | 0.15 | 0.50 | 0.6500 | 0.7750 | 0.7750 | 0.7125 |

Selected weights: `{'lexical': 0.4, 'vector': 0.1, 'symbol': 0.5}`

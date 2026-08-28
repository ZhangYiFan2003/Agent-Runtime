# Retrieval v1 to v2 Comparison

- Queries: 80
- V1 evidence pack commit: `542952444ca2d9c40a865c828481a2bcf1809251`
- Artifact-recorded source commit: `2516518260894f350aa4e3df9ed937ff2d382d0f`
- V1 metrics are read directly from the committed immutable artifact.

| Mode | Metric | V1 | V2 | Absolute delta | Relative delta |
| --- | --- | ---: | ---: | ---: | ---: |
| lexical | recall_at_1 | 0.3750 | 0.5125 | +0.1375 | +0.3667 |
| lexical | recall_at_3 | 0.4375 | 0.6875 | +0.2500 | +0.5714 |
| lexical | recall_at_5 | 0.4625 | 0.7250 | +0.2625 | +0.5676 |
| lexical | mrr_at_5 | 0.4077 | 0.5998 | +0.1921 | +0.4712 |
| hybrid | recall_at_1 | 0.4250 | 0.6250 | +0.2000 | +0.4706 |
| hybrid | recall_at_3 | 0.5125 | 0.7500 | +0.2375 | +0.4634 |
| hybrid | recall_at_5 | 0.5125 | 0.7625 | +0.2500 | +0.4878 |
| hybrid | mrr_at_5 | 0.4646 | 0.6858 | +0.2212 | +0.4761 |

## Hybrid category comparison

| Query type | V1 Recall@5 | V2 Recall@5 | Delta | V1 MRR@5 | V2 MRR@5 | Delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| graph/context-oriented | 0.2500 | 1.0000 | +0.7500 | 0.2000 | 0.8500 | +0.6500 |
| lexical-oriented | 0.8500 | 0.8000 | -0.0500 | 0.7083 | 0.7750 | +0.0667 |
| semantic/paraphrase-oriented | 0.0000 | 0.2500 | +0.2500 | 0.0000 | 0.1183 | +0.1183 |
| symbol-oriented | 0.9500 | 1.0000 | +0.0500 | 0.9500 | 1.0000 | +0.0500 |

## Same-v2-corpus latency

Latency is not available in the committed v1 artifact. The values below compare old and new modes against the same v2 source corpus.

- Hybrid v1 p50/p95: 158.7101 / 194.4150 ms
- Hybrid v2 p50/p95: 204.3882 / 242.8995 ms

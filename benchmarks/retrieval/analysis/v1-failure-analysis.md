# Retrieval v1 Failure Analysis

- Queries: 80
- Hybrid Top-5 misses: 39
- Git commit: `542952444ca2d9c40a865c828481a2bcf1809251`

## Candidate pool diagnosis

| Outcome | Count |
| --- | ---: |
| candidate_generation_miss | 3 |
| present_but_ranked_too_low | 36 |

## Primary failure classification

| Failure type | Count |
| --- | ---: |
| correct candidate exists but ranking too low | 1 |
| lexical mismatch | 34 |
| semantic mismatch | 4 |

## Candidate recall

- Candidate Recall@20: 0.7750
- Candidate Recall@50: 0.8000

## Miss cases

| ID | Query type | Failure | Candidate status | First Hybrid rank after 5 |
| --- | --- | --- | --- | ---: |
| symbol_001 | symbol-oriented | semantic mismatch | present_but_ranked_too_low | 32 |
| lexical_002 | lexical-oriented | correct candidate exists but ranking too low | present_but_ranked_too_low | 7 |
| lexical_011 | lexical-oriented | lexical mismatch | present_but_ranked_too_low | 19 |
| lexical_014 | lexical-oriented | lexical mismatch | present_but_ranked_too_low | 14 |
| semantic_001 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 128 |
| semantic_002 | semantic/paraphrase-oriented | semantic mismatch | candidate_generation_miss | - |
| semantic_003 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 93 |
| semantic_004 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 60 |
| semantic_005 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 31 |
| semantic_006 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 96 |
| semantic_007 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 173 |
| semantic_008 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 109 |
| semantic_009 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 121 |
| semantic_010 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 194 |
| semantic_011 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 16 |
| semantic_012 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 59 |
| semantic_013 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 128 |
| semantic_014 | semantic/paraphrase-oriented | semantic mismatch | candidate_generation_miss | - |
| semantic_015 | semantic/paraphrase-oriented | semantic mismatch | candidate_generation_miss | - |
| semantic_016 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 29 |
| semantic_017 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 14 |
| semantic_018 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 110 |
| semantic_019 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 33 |
| semantic_020 | semantic/paraphrase-oriented | lexical mismatch | present_but_ranked_too_low | 13 |
| graph_001 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 20 |
| graph_002 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 31 |
| graph_003 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 24 |
| graph_004 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 8 |
| graph_005 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 16 |
| graph_007 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 9 |
| graph_008 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 49 |
| graph_009 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 45 |
| graph_010 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 40 |
| graph_011 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 16 |
| graph_013 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 104 |
| graph_015 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 61 |
| graph_016 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 15 |
| graph_017 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 22 |
| graph_018 | graph/context-oriented | lexical mismatch | present_but_ranked_too_low | 97 |

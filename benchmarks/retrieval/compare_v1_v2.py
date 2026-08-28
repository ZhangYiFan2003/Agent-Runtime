from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from benchmarks.retrieval.evaluation import serialize_report

METRICS = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr_at_5")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare committed v1 retrieval evidence with v2")
    parser.add_argument(
        "--v1",
        type=Path,
        default=Path("benchmarks/retrieval/results/retrieval-offline-baseline.json"),
    )
    parser.add_argument(
        "--v2",
        type=Path,
        default=Path("benchmarks/retrieval/results/retrieval-v2-v1-analysis.json"),
    )
    parser.add_argument(
        "--v1-analysis",
        type=Path,
        default=Path("benchmarks/retrieval/analysis/v1-failure-analysis.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/retrieval/results/retrieval-v1-v2-comparison.json"),
    )
    args = parser.parse_args()

    v1 = _read(args.v1)
    v2 = _read(args.v2)
    analysis = _read(args.v1_analysis)
    if v1["dataset"]["query_count"] != v2["dataset"]["query_count"]:
        raise ValueError("v1 and v2 query counts differ")
    if v1["dataset"]["version"] != v2["dataset"]["version"]:
        raise ValueError("v1 and v2 dataset versions differ")

    report = {
        "schema_version": 1,
        "comparison": "committed-v1-to-frozen-v2",
        "dataset": {
            "version": v1["dataset"]["version"],
            "query_count": v1["dataset"]["query_count"],
            "v2_sha256": v2["dataset"]["sha256"],
        },
        "commits": {
            "v1_artifact_recorded_source": v1["environment"]["git_commit"],
            "v1_evidence_pack_commit": v2["environment"]["git_commit"],
            "v2_worktree_head": v2["environment"]["git_commit"],
            "v2_worktree_dirty": v2["environment"]["working_tree_dirty"],
        },
        "overall": {
            "lexical": _compare(v1["modes"]["lexical"]["all"], v2["modes"]["lexical_v2"]["all"]),
            "hybrid": _compare(v1["modes"]["hybrid"]["all"], v2["modes"]["hybrid_v2"]["all"]),
        },
        "hybrid_by_query_type": {
            query_type: _compare(
                v1["modes"]["hybrid"]["by_query_type"][query_type],
                v2["modes"]["hybrid_v2"]["by_query_type"][query_type],
            )
            for query_type in v1["modes"]["hybrid"]["by_query_type"]
        },
        "candidate_recall": {
            "v1": analysis["candidate_recall"],
            "v2": v2["candidate_recall"]["v2_lexical_vector_symbol"],
        },
        "graph_context": {
            "v1": v1["graph_aware_context"],
            "v2": v2["graph_aware_context_v2"],
        },
        "same_v2_corpus_latency_ms": {
            "hybrid_v1": v2["latency_ms"]["hybrid_v1"],
            "hybrid_v2": v2["latency_ms"]["hybrid_v2"],
        },
        "integrity": {
            "v1_source": args.v1.as_posix(),
            "v2_source": args.v2.as_posix(),
            "note": (
                "v1 values are read from the committed immutable artifact; "
                "no result values are edited"
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialize_report(report), encoding="utf-8")
    args.output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(_markdown(report))


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"benchmark artifact must be an object: {path}")
    return value


def _compare(before: dict[str, float], after: dict[str, float]) -> dict[str, Any]:
    return {
        metric: {
            "v1": before[metric],
            "v2": after[metric],
            "absolute_delta": round(after[metric] - before[metric], 4),
            "relative_delta": round((after[metric] - before[metric]) / before[metric], 4)
            if before[metric]
            else None,
        }
        for metric in METRICS
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Retrieval v1 to v2 Comparison",
        "",
        f"- Queries: {report['dataset']['query_count']}",
        f"- V1 evidence pack commit: `{report['commits']['v1_evidence_pack_commit']}`",
        f"- Artifact-recorded source commit: `{report['commits']['v1_artifact_recorded_source']}`",
        "- V1 metrics are read directly from the committed immutable artifact.",
        "",
        "| Mode | Metric | V1 | V2 | Absolute delta | Relative delta |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for mode, metrics in report["overall"].items():
        for metric, values in metrics.items():
            relative = values["relative_delta"]
            relative_text = f"{relative:+.4f}" if relative is not None else "n/a"
            lines.append(
                f"| {mode} | {metric} | {values['v1']:.4f} | {values['v2']:.4f} | "
                f"{values['absolute_delta']:+.4f} | {relative_text} |"
            )
    lines.extend(
        [
            "",
            "## Hybrid category comparison",
            "",
            "| Query type | V1 Recall@5 | V2 Recall@5 | Delta | V1 MRR@5 | V2 MRR@5 | Delta |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for query_type, metrics in report["hybrid_by_query_type"].items():
        recall = metrics["recall_at_5"]
        mrr = metrics["mrr_at_5"]
        lines.append(
            f"| {query_type} | {recall['v1']:.4f} | {recall['v2']:.4f} | "
            f"{recall['absolute_delta']:+.4f} | {mrr['v1']:.4f} | {mrr['v2']:.4f} | "
            f"{mrr['absolute_delta']:+.4f} |"
        )
    lines.extend(
        [
            "",
            "## Same-v2-corpus latency",
            "",
            "Latency is not available in the committed v1 artifact. The values below compare old "
            "and new modes against the same v2 source corpus.",
            "",
            f"- Hybrid v1 p50/p95: {report['same_v2_corpus_latency_ms']['hybrid_v1']['p50']:.4f} / "
            f"{report['same_v2_corpus_latency_ms']['hybrid_v1']['p95']:.4f} ms",
            f"- Hybrid v2 p50/p95: {report['same_v2_corpus_latency_ms']['hybrid_v2']['p50']:.4f} / "
            f"{report['same_v2_corpus_latency_ms']['hybrid_v2']['p95']:.4f} ms",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()

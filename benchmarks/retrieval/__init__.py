"""Repository-grounded retrieval evidence benchmark."""

from benchmarks.retrieval.evaluation import (
    DeterministicHashEmbeddingProvider,
    RetrievalCase,
    RetrievalDataset,
    evaluate_by_query_type,
    evaluate_candidate_recall,
    evaluate_rankings,
    first_relevant_rank,
    load_retrieval_dataset,
    normalize_query_identity,
    percentile,
    result_matches_target,
    serialize_report,
    validate_dataset_splits,
)

__all__ = [
    "DeterministicHashEmbeddingProvider",
    "RetrievalCase",
    "RetrievalDataset",
    "evaluate_candidate_recall",
    "evaluate_by_query_type",
    "evaluate_rankings",
    "first_relevant_rank",
    "load_retrieval_dataset",
    "normalize_query_identity",
    "percentile",
    "result_matches_target",
    "serialize_report",
    "validate_dataset_splits",
]

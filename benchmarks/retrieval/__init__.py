"""Repository-grounded retrieval evidence benchmark."""

from benchmarks.retrieval.evaluation import (
    DeterministicHashEmbeddingProvider,
    RetrievalCase,
    RetrievalDataset,
    evaluate_rankings,
    load_retrieval_dataset,
    serialize_report,
)

__all__ = [
    "DeterministicHashEmbeddingProvider",
    "RetrievalCase",
    "RetrievalDataset",
    "evaluate_rankings",
    "load_retrieval_dataset",
    "serialize_report",
]

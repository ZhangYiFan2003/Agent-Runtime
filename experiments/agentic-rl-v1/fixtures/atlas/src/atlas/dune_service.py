from __future__ import annotations

CONFIG_KEY = "dune_retry_limit"


class DuneService:
    """Deterministic dune processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:dune:" + self.normalize(payload)


def dune_pipeline(payload: str) -> str:
    return DuneService().run(payload)

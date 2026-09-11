from __future__ import annotations

CONFIG_KEY = "iris_retry_limit"


class IrisService:
    """Deterministic iris processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:iris:" + self.normalize(payload)


def iris_pipeline(payload: str) -> str:
    return IrisService().run(payload)

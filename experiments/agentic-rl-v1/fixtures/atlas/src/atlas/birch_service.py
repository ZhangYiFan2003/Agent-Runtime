from __future__ import annotations

CONFIG_KEY = "birch_retry_limit"


class BirchService:
    """Deterministic birch processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:birch:" + self.normalize(payload)


def birch_pipeline(payload: str) -> str:
    return BirchService().run(payload)

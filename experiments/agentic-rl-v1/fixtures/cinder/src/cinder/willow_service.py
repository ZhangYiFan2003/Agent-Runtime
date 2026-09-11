from __future__ import annotations

CONFIG_KEY = "willow_retry_limit"


class WillowService:
    """Deterministic willow processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "cinder:willow:" + self.normalize(payload)


def willow_pipeline(payload: str) -> str:
    return WillowService().run(payload)

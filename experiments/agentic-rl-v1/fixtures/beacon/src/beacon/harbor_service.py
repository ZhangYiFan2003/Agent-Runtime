from __future__ import annotations

CONFIG_KEY = "harbor_retry_limit"


class HarborService:
    """Deterministic harbor processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:harbor:" + self.normalize(payload)


def harbor_pipeline(payload: str) -> str:
    return HarborService().run(payload)

from __future__ import annotations

CONFIG_KEY = "amber_retry_limit"


class AmberService:
    """Deterministic amber processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:amber:" + self.normalize(payload)


def amber_pipeline(payload: str) -> str:
    return AmberService().run(payload)

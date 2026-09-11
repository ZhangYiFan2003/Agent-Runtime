from __future__ import annotations

CONFIG_KEY = "ember_retry_limit"


class EmberService:
    """Deterministic ember processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:ember:" + self.normalize(payload)


def ember_pipeline(payload: str) -> str:
    return EmberService().run(payload)

from __future__ import annotations

CONFIG_KEY = "pine_retry_limit"


class PineService:
    """Deterministic pine processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:pine:" + self.normalize(payload)


def pine_pipeline(payload: str) -> str:
    return PineService().run(payload)

from __future__ import annotations

CONFIG_KEY = "summit_retry_limit"


class SummitService:
    """Deterministic summit processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:summit:" + self.normalize(payload)


def summit_pipeline(payload: str) -> str:
    return SummitService().run(payload)

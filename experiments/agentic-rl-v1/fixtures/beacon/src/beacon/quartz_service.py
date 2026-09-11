from __future__ import annotations

CONFIG_KEY = "quartz_retry_limit"


class QuartzService:
    """Deterministic quartz processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:quartz:" + self.normalize(payload)


def quartz_pipeline(payload: str) -> str:
    return QuartzService().run(payload)

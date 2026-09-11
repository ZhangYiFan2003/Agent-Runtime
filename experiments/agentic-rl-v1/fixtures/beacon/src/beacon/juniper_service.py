from __future__ import annotations

CONFIG_KEY = "juniper_retry_limit"


class JuniperService:
    """Deterministic juniper processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:juniper:" + self.normalize(payload)


def juniper_pipeline(payload: str) -> str:
    return JuniperService().run(payload)

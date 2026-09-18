from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.policy.redaction import redact_secrets


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def record(
        self,
        *,
        tool_name: str,
        input_data: dict[str, Any],
        outcome: str,
        approver: str,
        cwd: str,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "input": redact_secrets(input_data),
            "outcome": outcome,
            "approver": approver,
            "cwd": cwd,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

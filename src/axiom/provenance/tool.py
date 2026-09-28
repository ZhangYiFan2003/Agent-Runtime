from __future__ import annotations

from typing import Any

from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema


def provenance_tools() -> list[Tool]:
    return [
        Tool(
            name="record_claim",
            description=(
                "Record a material conclusion with durable Runtime evidence and return a "
                "[claim:...] citation marker. Evidence must reference real Tool executions, "
                "Artifacts, code locations, or Trace spans."
            ),
            parameters=object_schema(
                {
                    "text": {"type": "string", "description": "Specific conclusion"},
                    "claim_kind": {"type": "string", "description": "Optional short category"},
                    "evidence": {
                        "type": "array",
                        "description": "Runtime evidence descriptors",
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "type": {"type": "string"},
                                "invocation_id": {"type": "string"},
                                "tool_call_id": {"type": "string"},
                                "artifact_id": {"type": "string"},
                                "span_id": {"type": "string"},
                                "run_id": {"type": "string"},
                                "path": {"type": "string"},
                                "start_line": {"type": "integer"},
                                "end_line": {"type": "integer"},
                            },
                            "required": ["type"],
                        },
                    },
                },
                ["text", "evidence"],
            ),
            required_keys=["text", "evidence"],
            handler=record_claim,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="safe",
            retry_safety="idempotent",
        )
    ]


async def record_claim(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    service = context.provenance_service
    if service is None:
        return ToolResult("Provenance is not enabled.", is_error=True)
    if not all((context.invocation_id, context.run_id, context.thread_id, context.turn_id)):
        return ToolResult("Claim recording requires a durable Run context.", is_error=True)
    descriptors = payload.get("evidence")
    if not isinstance(descriptors, list) or any(not isinstance(item, dict) for item in descriptors):
        return ToolResult("record_claim evidence must be a list of objects.", is_error=True)
    try:
        claim, evidence = await service.record_claim(
            invocation_id=context.invocation_id,
            run_id=context.run_id,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            text=str(payload["text"]),
            claim_kind=str(payload["claim_kind"]) if payload.get("claim_kind") else None,
            evidence_descriptors=descriptors,
        )
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(f"Claim not recorded: {exc}", is_error=True)
    content = (
        f"Recorded claim {claim.claim_id} with {len(evidence)} evidence sources.\n"
        f"Citation: {claim.citation_marker}"
    )
    return ToolResult(
        content,
        display_summary=f"Recorded claim {claim.claim_id}",
        metadata={
            "claim_id": claim.claim_id,
            "evidence_count": len(evidence),
            "citation_marker": claim.citation_marker,
            "provenance.claim_id": claim.claim_id,
            "provenance.evidence_count": len(evidence),
        },
    )

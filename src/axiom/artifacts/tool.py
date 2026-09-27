from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from axiom.policy import Capability, PathGuard
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema


def artifact_tools() -> list[Tool]:
    return [
        Tool(
            name="publish_artifact",
            description="Publish an existing workspace file as an immutable Runtime Artifact.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Workspace file to publish"},
                    "name": {"type": "string", "description": "Optional download filename"},
                    "media_type": {"type": "string", "description": "Optional MIME type"},
                },
                ["path"],
            ),
            required_keys=["path"],
            handler=publish_artifact,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            retry_safety="idempotent",
            capabilities=(
                Capability.FILESYSTEM_READ.value,
                Capability.EXTERNAL_SIDE_EFFECT.value,
            ),
            path_argument_names=("path",),
        )
    ]


async def publish_artifact(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if context.artifact_service is None:
        return ToolResult("Artifact Store is not enabled.", is_error=True)
    if not context.run_id or not context.thread_id or not context.invocation_id:
        return ToolResult("Artifact publishing requires a durable Run context.", is_error=True)
    path = PathGuard(context.workspace or context.cwd).validate(str(payload["path"]))
    if not path.is_file():
        return ToolResult("publish_artifact requires an existing file.", is_error=True)
    relative = str(path.relative_to(Path(context.workspace or context.cwd).resolve()))
    artifact, reused_blob = await asyncio.to_thread(
        context.artifact_service.publish_file,
        path,
        run_id=context.run_id,
        thread_id=context.thread_id,
        invocation_id=context.invocation_id,
        tool_name="publish_artifact",
        name=str(payload.get("name") or path.name),
        media_type=str(payload["media_type"]) if payload.get("media_type") else None,
        source_path=relative,
        kind="file",
    )
    summary = (
        f"Published artifact {artifact.artifact_id}\n"
        f"name: {artifact.name}\n"
        f"size: {artifact.size_bytes} bytes\n"
        f"sha256: {artifact.blob_sha256}"
    )
    return ToolResult(
        summary,
        display_summary=f"Published {artifact.name}",
        metadata={
            "artifact_ids": [artifact.artifact_id],
            "artifact.id": artifact.artifact_id,
            "artifact.name": artifact.name,
            "artifact.sha256": artifact.blob_sha256,
            "artifact.size_bytes": artifact.size_bytes,
            "artifact.reused_blob": reused_blob,
        },
    )

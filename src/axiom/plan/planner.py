from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from axiom.llm.base import LlmClient
from axiom.plan.models import ExecutionPlan, Task, TaskType
from axiom.types import Message

PLANNER_PROMPT = """You are Axiom Agent Runtime's planner.
Create a compact executable DAG for the user's task.
Return only JSON with this shape:
{
  "summary": "short summary",
  "tasks": [
    {
      "id": "stable_source_id",
      "description": "concrete executable step",
      "type": "FILE_READ|FILE_WRITE|COMMAND|ANALYSIS|VERIFICATION",
      "dependencies": ["stable_source_id"]
    }
  ]
}
Use independent tasks when they can run in parallel.
"""


@dataclass(frozen=True, slots=True)
class PlannerResult:
    plan: ExecutionPlan
    used_llm: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "end_turn"
    ttft_ms: float | None = None


class Planner:
    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def requires_llm(self, goal: str) -> bool:
        return not _is_simple_goal(goal)

    async def create_plan(self, goal: str) -> ExecutionPlan:
        return (await self.create_plan_result(goal)).plan

    async def create_plan_result(self, goal: str) -> PlannerResult:
        if _is_simple_goal(goal):
            return PlannerResult(plan=_minimal_plan(goal), used_llm=False)
        response = await _collect_text(
            self.llm_client,
            [Message(role="user", content=f"Please create an execution plan for:\n{goal}")],
            system_prompt=PLANNER_PROMPT,
        )
        return PlannerResult(
            plan=self.parse_plan(goal, response.text),
            used_llm=True,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            finish_reason=response.finish_reason,
            ttft_ms=response.ttft_ms,
        )

    async def replan(self, failed_plan: ExecutionPlan, failure_reason: str) -> ExecutionPlan:
        return (await self.replan_result(failed_plan, failure_reason)).plan

    async def replan_result(
        self,
        failed_plan: ExecutionPlan,
        failure_reason: str,
    ) -> PlannerResult:
        completed = "\n".join(
            f"- {task.id}: {task.description}"
            for task in failed_plan.all_tasks()
            if task.result and not task.error
        )
        return await self.create_plan_result(
            f"{failed_plan.goal}\nFailure reason: {failure_reason}\nCompleted tasks:\n{completed}"
        )

    def parse_plan(self, goal: str, plan_json: str) -> ExecutionPlan:
        data = _parse_json_object(plan_json)
        task_nodes = data.get("tasks") or data.get("steps") or []
        if not isinstance(task_nodes, list) or not task_nodes:
            raise ValueError("planner output did not contain a non-empty tasks/steps array")

        plan = ExecutionPlan(id=f"plan_{int(time.time() * 1000)}", goal=goal)
        plan.summary = str(data.get("summary") or "")
        id_mapping: dict[str, str] = {}

        for index, node in enumerate(task_nodes, start=1):
            if not isinstance(node, dict):
                continue
            original_id = str(node.get("id") or f"task_{index}")
            new_id = f"task_{index}"
            id_mapping[original_id] = new_id
            plan.add_task(
                Task(
                    id=new_id,
                    description=str(node.get("description") or original_id),
                    type=_parse_task_type(str(node.get("type") or "ANALYSIS")),
                )
            )

        for index, node in enumerate(task_nodes, start=1):
            if not isinstance(node, dict):
                continue
            task = plan.get_task(f"task_{index}")
            if not task:
                continue
            dependencies = node.get("dependencies") or []
            if not isinstance(dependencies, list):
                continue
            for raw_dep in dependencies:
                dep_id = id_mapping.get(str(raw_dep), str(raw_dep))
                if dep_id in plan.tasks:
                    task.add_dependency(dep_id)
                    plan.tasks[dep_id].add_dependent(task.id)

        if not plan.compute_execution_order():
            raise ValueError("plan contains a cyclic dependency")
        return plan


async def _collect_text(
    llm_client: LlmClient,
    messages: list[Message],
    *,
    system_prompt: str,
) -> _PlannerResponse:
    text = ""
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason = "end_turn"
    started = time.perf_counter()
    ttft_ms: float | None = None
    async for event in llm_client.chat(messages, [], system_prompt=system_prompt):
        event_type = event.get("type")
        if event_type == "text_delta":
            if ttft_ms is None:
                ttft_ms = round((time.perf_counter() - started) * 1000, 3)
            text += str(event.get("text") or "")
        elif event_type == "usage":
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                prompt_tokens += int(usage.get("input_tokens") or 0)
                completion_tokens += int(usage.get("output_tokens") or 0)
        elif event_type == "message_end":
            finish_reason = str(event.get("stop_reason") or "end_turn")
        elif event_type == "error":
            raise event["error"]
    return _PlannerResponse(
        text=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        ttft_ms=ttft_ms,
    )


@dataclass(frozen=True, slots=True)
class _PlannerResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    ttft_ms: float | None


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?\s*", "", text or "").replace("```", "").strip()
    if not cleaned:
        raise ValueError("empty planner output")
    return json.loads(cleaned)


def _parse_task_type(value: str) -> TaskType:
    normalized = value.upper()
    try:
        return TaskType(normalized)
    except ValueError:
        return TaskType.ANALYSIS


def _is_simple_goal(goal: str | None) -> bool:
    normalized = (goal or "").strip()
    if not normalized or len(normalized) > 30:
        return False
    multi_step_cues = ["然后", "并且", "再", "最后", "同时", "先", "之后", "接着", "以及"]
    if any(cue in normalized for cue in multi_step_cues):
        return False
    simple_cues = ["列出", "查看", "读取", "显示", "执行", "运行", "搜索", "当前目录", "文件"]
    return any(cue in normalized for cue in simple_cues)


def _minimal_plan(goal: str) -> ExecutionPlan:
    normalized = goal.strip()
    plan = ExecutionPlan(id=f"plan_{int(time.time() * 1000)}", goal=normalized)
    plan.summary = f"直接执行简单任务：{normalized}"
    plan.add_task(Task(id="task_1", description=normalized, type=_infer_simple_type(normalized)))
    plan.compute_execution_order()
    return plan


def _infer_simple_type(goal: str) -> TaskType:
    if any(token in goal for token in ["读取", "打开", "查看"]) and "文件" in goal:
        return TaskType.FILE_READ
    if any(token in goal for token in ["写入", "修改", "创建文件"]):
        return TaskType.FILE_WRITE
    if any(token in goal for token in ["分析", "总结", "解释"]):
        return TaskType.ANALYSIS
    if any(token in goal for token in ["验证", "检查"]):
        return TaskType.VERIFICATION
    return TaskType.COMMAND

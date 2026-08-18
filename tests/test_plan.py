from __future__ import annotations

import asyncio
from typing import Any

from axiom.agent import PlanExecuteAgent
from axiom.config import load_config
from axiom.plan import ExecutionPlan, Planner, Task, TaskType
from axiom.tools import ToolRegistry, get_builtin_tools


def test_execution_plan_exposes_dag_batches():
    plan = ExecutionPlan(id="plan_1", goal="demo")
    task_1 = Task("task_1", "read a", TaskType.FILE_READ)
    task_2 = Task("task_2", "read b", TaskType.FILE_READ)
    task_3 = Task("task_3", "summarize", TaskType.ANALYSIS, ["task_1", "task_2"])

    plan.add_task(task_1)
    plan.add_task(task_2)
    plan.add_task(task_3)

    assert plan.execution_order() == ["task_1", "task_2", "task_3"]
    assert plan.execution_batches() == [[task_1, task_2], [task_3]]
    assert plan.executable_tasks() == [task_1, task_2]
    task_1.mark_completed("done")
    assert plan.executable_tasks() == [task_2]


def test_planner_parses_tasks_and_dependencies():
    planner = Planner(FakeClient())

    plan = planner.parse_plan(
        "demo",
        """
        ```json
        {
          "summary": "demo plan",
          "tasks": [
            {"id": "a", "description": "A", "type": "COMMAND", "dependencies": []},
            {"id": "b", "description": "B", "type": "VERIFICATION", "dependencies": ["a"]}
          ]
        }
        ```
        """,
    )

    assert plan.summary == "demo plan"
    assert plan.get_task("task_2").dependencies == ["task_1"]
    assert plan.get_task("task_2").type == TaskType.VERIFICATION


def test_plan_execute_runs_independent_tasks_at_durable_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    client = ParallelPlanClient()
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    agent = PlanExecuteAgent(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run():
        text = ""
        async for event in agent.run("先做 A 和 B，然后汇总"):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "error":
                raise event["error"]
        return text

    result = asyncio.run(run())

    assert "Completed [task_1]" in result
    assert "Completed [task_2]" in result
    assert client.peak_concurrency == 1
    assert client.starts == ["Task A", "Task B"]


class FakeClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        yield {"type": "text_delta", "text": "{}"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ParallelPlanClient(FakeClient):
    def __init__(self):
        self.current_concurrency = 0
        self.peak_concurrency = 0
        self.starts: list[str] = []

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Please create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"parallel","tasks":['
                    '{"id":"a","description":"Task A","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"Task B","type":"ANALYSIS","dependencies":[]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        if "Task A" in body or "Task B" in body:
            name = "Task A" if "Current task [task_1]" in body else "Task B"
            self.starts.append(name)
            self.current_concurrency += 1
            self.peak_concurrency = max(self.peak_concurrency, self.current_concurrency)
            await asyncio.sleep(0)
            self.current_concurrency -= 1
            text = "result for A" if name == "Task A" else "result for B"
            yield {"type": "text_delta", "text": text}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content)


def test_plan_execute_runs_dependent_task_after_dependencies_and_injects_results(tmp_path):
    client = DependentPlanClient()
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    agent = PlanExecuteAgent(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run():
        events = []
        async for event in agent.run("run A and B before C"):
            if event.get("type") == "error":
                raise event["error"]
            events.append(event)
        return events

    events = asyncio.run(run())
    text = "".join(str(event.get("text") or "") for event in events)
    done = events[-1]

    assert client.peak_concurrency == 1
    assert client.starts[:2] == ["Task A", "Task B"]
    assert client.starts[-1] == "Task C"
    assert set(client.completed_before_c) == {"Task A", "Task B"}
    assert "Result A" in client.task_c_context
    assert "Result B" in client.task_c_context
    assert "Completed [task_3]" in text
    assert "Plan execution completed" in text
    assert done["total_turns"] == 3
    assert done["total_tokens"] == 6


def test_plan_execute_propagates_worker_failure_without_hiding_successes(tmp_path):
    client = FailingPlanClient()
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    agent = PlanExecuteAgent(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run():
        events = []
        async for event in agent.run("run one successful and one failing task"):
            events.append(event)
        return events

    events = asyncio.run(run())
    text = "".join(str(event.get("text") or "") for event in events)
    assert any(event.get("type") == "error" for event in events)
    assert "Failed [task_1]: worker boom" in text
    assert "Completed [task_2]" in text


class DependentPlanClient(FakeClient):
    def __init__(self):
        self.current_concurrency = 0
        self.peak_concurrency = 0
        self.starts: list[str] = []
        self.finished: list[str] = []
        self.starts: list[str] = []
        self.completed_before_c: list[str] = []
        self.task_c_context = ""

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Please create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"dependent","tasks":['
                    '{"id":"a","description":"Task A","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"Task B","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"c","description":"Task C","type":"ANALYSIS","dependencies":["a","b"]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        if "Task A" in body and "Current task [task_1]" in body:
            async for event in self._parallel_task("Task A", "Result A"):
                yield event
            return
        if "Task B" in body and "Current task [task_2]" in body:
            async for event in self._parallel_task("Task B", "Result B"):
                yield event
            return
        if "Current task [task_3]" in body:
            self.starts.append("Task C")
            self.completed_before_c = list(self.finished)
            self.task_c_context = body
            yield {"type": "text_delta", "text": "Result C"}
            yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}

    async def _parallel_task(self, name: str, result: str):
        self.starts.append(name)
        self.current_concurrency += 1
        self.peak_concurrency = max(self.peak_concurrency, self.current_concurrency)
        await asyncio.sleep(0)
        self.current_concurrency -= 1
        self.finished.append(name)
        yield {"type": "text_delta", "text": result}
        yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class FailingPlanClient(FakeClient):
    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Please create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"failure","tasks":['
                    '{"id":"a","description":"Failing task","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"Successful task","type":"ANALYSIS","dependencies":[]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Failing task" in body:
            yield {"type": "error", "error": RuntimeError("worker boom")}
            return
        if "Successful task" in body:
            yield {"type": "text_delta", "text": "Stable result"}
            yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}

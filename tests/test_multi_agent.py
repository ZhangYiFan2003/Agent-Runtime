from __future__ import annotations

import asyncio
from typing import Any

from axiom.agent.orchestrator import AgentOrchestrator
from axiom.config import load_config
from axiom.tools import ToolRegistry, get_builtin_tools


def test_orchestrator_parses_steps_and_review_output(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    orchestrator = _orchestrator(tmp_path, FakeTeamClient())

    steps = orchestrator.parse_plan(
        """
        {"steps": [
          {"id": "a", "description": "A", "type": "ANALYSIS", "dependencies": []},
          {"id": "b", "description": "B", "type": "COMMAND", "dependencies": ["a"]}
        ]}
        """
    )

    assert [step.id for step in steps] == ["step_1", "step_2"]
    assert steps[1].dependencies == ["step_1"]
    assert orchestrator.parse_review_approval('{"approved": true, "issues": []}')
    assert not orchestrator.parse_review_approval("执行结果未通过审查")
    assert "缺少验证" in orchestrator.parse_review_issues(
        '{"approved": false, "issues": ["缺少验证"]}'
    )


def test_orchestrator_runs_independent_workers_sequentially(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    client = ParallelTeamClient()
    orchestrator = _orchestrator(tmp_path, client)

    async def run():
        text = ""
        async for event in orchestrator.run("并行完成 A 和 B"):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "error":
                raise event["error"]
        return text

    result = asyncio.run(run())

    assert "Multi-Agent task completed" in result
    assert "Task A result" in result
    assert "Task B result" in result
    assert client.peak_concurrency == 1


class FakeTeamClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        yield {"type": "text_delta", "text": "{}"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ParallelTeamClient(FakeTeamClient):
    def __init__(self):
        self.current_concurrency = 0
        self.peak_concurrency = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Original task" in body:
            yield {"type": "text_delta", "text": '{"approved": true, "issues": []}'}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"parallel","steps":['
                    '{"id":"a","description":"Task A","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"Task B","type":"ANALYSIS","dependencies":[]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Task A" in body or "Task B" in body:
            self.current_concurrency += 1
            self.peak_concurrency = max(self.peak_concurrency, self.current_concurrency)
            await asyncio.sleep(0)
            self.current_concurrency -= 1
            text = "Task A result" if "Task A" in body else "Task B result"
            yield {"type": "text_delta", "text": text}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _orchestrator(tmp_path, client):
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    return AgentOrchestrator(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content)


def test_orchestrator_runs_dependent_worker_after_dependency_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    client = DependentTeamClient()
    orchestrator = _orchestrator(tmp_path, client)

    async def run():
        text = ""
        async for event in orchestrator.run("run A before B"):
            if event.get("type") == "error":
                raise event["error"]
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
        return text

    result = asyncio.run(run())

    assert client.worker_starts == ["Task A", "Task B"]
    assert "Task A result" in client.task_b_context
    assert "Multi-Agent task completed" in result
    assert "Task A result" in result
    assert "Task B result" in result


def test_orchestrator_retries_when_reviewer_rejects_then_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    client = RetryReviewTeamClient()
    orchestrator = _orchestrator(tmp_path, client)

    async def run():
        text = ""
        async for event in orchestrator.run("review and retry"):
            if event.get("type") == "error":
                raise event["error"]
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
        return text

    result = asyncio.run(run())

    assert client.worker_attempts == 2
    assert client.review_attempts == 2
    assert "fixed result" in result
    assert "draft result" not in result
    assert "COMPLETED" in result


def test_orchestrator_reports_worker_failure_in_final_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    client = FailingTeamClient()
    orchestrator = _orchestrator(tmp_path, client)

    async def run():
        text = ""
        async for event in orchestrator.run("one worker fails"):
            if event.get("type") == "error":
                raise event["error"]
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
        return text

    result = asyncio.run(run())

    assert "did not fully complete" in result
    assert "FAILED: Failing Task" in result
    assert "worker failed intentionally" in result
    assert "COMPLETED: Successful Task" in result
    assert "success result" in result


class DependentTeamClient(FakeTeamClient):
    def __init__(self):
        self.worker_starts: list[str] = []
        self.task_b_context = ""

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Original task" in body:
            yield {"type": "text_delta", "text": '{"approved": true, "issues": []}'}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"dependent","steps":['
                    '{"id":"a","description":"Task A","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"Task B","type":"ANALYSIS","dependencies":["a"]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Current task:\nTask A" in body:
            self.worker_starts.append("Task A")
            yield {"type": "text_delta", "text": "Task A result"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Current task:\nTask B" in body:
            self.worker_starts.append("Task B")
            self.task_b_context = body
            yield {"type": "text_delta", "text": "Task B result"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class RetryReviewTeamClient(FakeTeamClient):
    def __init__(self):
        self.worker_attempts = 0
        self.review_attempts = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"retry","steps":['
                    '{"id":"a","description":"Task A","type":"ANALYSIS","dependencies":[]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Original task" in body:
            self.review_attempts += 1
            if self.review_attempts == 1:
                yield {
                    "type": "text_delta",
                    "text": '{"approved": false, "summary": "needs fix", "issues": ["revise"]}',
                }
            else:
                yield {"type": "text_delta", "text": '{"approved": true, "issues": []}'}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Current task:\nTask A" in body:
            self.worker_attempts += 1
            text = "draft result" if self.worker_attempts == 1 else "fixed result"
            yield {"type": "text_delta", "text": text}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class FailingTeamClient(FakeTeamClient):
    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Original task" in body:
            yield {"type": "text_delta", "text": '{"approved": true, "issues": []}'}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"failure","steps":['
                    '{"id":"a","description":"Failing Task","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"Successful Task","type":"ANALYSIS","dependencies":[]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Failing Task" in body:
            yield {"type": "error", "error": RuntimeError("worker failed intentionally")}
            return
        if "Successful Task" in body:
            yield {"type": "text_delta", "text": "success result"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}

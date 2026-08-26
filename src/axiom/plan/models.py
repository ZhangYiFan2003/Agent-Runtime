from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PLAN_SCHEMA_VERSION = 2


class TaskType(StrEnum):
    PLANNING = "PLANNING"
    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    COMMAND = "COMMAND"
    ANALYSIS = "ANALYSIS"
    VERIFICATION = "VERIFICATION"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class PlanStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class Task:
    id: str
    description: str
    type: TaskType = TaskType.ANALYSIS
    dependencies: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    attempt: int = 0
    child_run_id: str | None = None
    reused_from: str | None = None
    execution_state: str = ""
    start_time: float = 0.0
    end_time: float = 0.0

    def add_dependency(self, task_id: str) -> None:
        if task_id not in self.dependencies:
            self.dependencies.append(task_id)

    def add_dependent(self, task_id: str) -> None:
        if task_id not in self.dependents:
            self.dependents.append(task_id)

    def mark_started(self) -> None:
        self.status = TaskStatus.RUNNING
        self.attempt += 1
        self.start_time = time.time()

    def mark_completed(self, result: str) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.end_time = time.time()

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error
        self.end_time = time.time()

    def mark_skipped(self) -> None:
        self.status = TaskStatus.SKIPPED
        self.end_time = time.time()

    def mark_cancelled(self, error: str = "cancelled") -> None:
        self.status = TaskStatus.CANCELLED
        self.error = error
        self.end_time = time.time()

    def is_executable(self, all_tasks: dict[str, Task]) -> bool:
        if self.status != TaskStatus.PENDING:
            return False
        return all(
            dep_id in all_tasks and all_tasks[dep_id].status == TaskStatus.COMPLETED
            for dep_id in self.dependencies
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "type": self.type.value,
            "dependencies": list(self.dependencies),
            "dependents": list(self.dependents),
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "attempt": self.attempt,
            "child_run_id": self.child_run_id,
            "reused_from": self.reused_from,
            "execution_state": self.execution_state,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=str(data["id"]),
            description=str(data.get("description") or ""),
            type=TaskType(str(data.get("type") or TaskType.ANALYSIS.value)),
            dependencies=_strings(data.get("dependencies")),
            dependents=_strings(data.get("dependents")),
            status=TaskStatus(str(data.get("status") or TaskStatus.PENDING.value)),
            result=str(data.get("result") or ""),
            error=str(data.get("error") or ""),
            attempt=int(data.get("attempt") or 0),
            child_run_id=_optional_string(data.get("child_run_id")),
            reused_from=_optional_string(data.get("reused_from")),
            execution_state=str(data.get("execution_state") or ""),
            start_time=float(data.get("start_time") or 0.0),
            end_time=float(data.get("end_time") or 0.0),
        )


@dataclass(slots=True)
class PlanVersion:
    version: int
    plan_id: str
    status: PlanStatus
    summary: str
    reason: str
    tasks: list[Task]
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "plan_id": self.plan_id,
            "status": self.status.value,
            "summary": self.summary,
            "reason": self.reason,
            "tasks": [task.to_dict() for task in self.tasks],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanVersion:
        raw_tasks = data.get("tasks")
        return cls(
            version=int(data.get("version") or 1),
            plan_id=str(data.get("plan_id") or ""),
            status=PlanStatus(str(data.get("status") or PlanStatus.CREATED.value)),
            summary=str(data.get("summary") or ""),
            reason=str(data.get("reason") or ""),
            tasks=[Task.from_dict(item) for item in raw_tasks if isinstance(item, dict)]
            if isinstance(raw_tasks, list)
            else [],
            created_at=float(data.get("created_at") or time.time()),
        )


@dataclass(slots=True)
class ExecutionPlan:
    id: str
    goal: str
    tasks: dict[str, Task] = field(default_factory=dict)
    status: PlanStatus = PlanStatus.CREATED
    schema_version: int = PLAN_SCHEMA_VERSION
    version: int = 1
    replan_count: int = 0
    history: list[PlanVersion] = field(default_factory=list)
    summary: str = ""
    created_at: float = field(default_factory=time.time)
    start_time: float = 0.0
    end_time: float = 0.0
    _execution_order: list[str] = field(default_factory=list)

    def add_task(self, task: Task) -> None:
        self.tasks[task.id] = task
        for dep_id in task.dependencies:
            dep = self.tasks.get(dep_id)
            if dep:
                dep.add_dependent(task.id)
        for existing in self.tasks.values():
            if task.id in existing.dependencies:
                task.add_dependent(existing.id)
        self._execution_order.clear()

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def all_tasks(self) -> list[Task]:
        return list(self.tasks.values())

    def root_tasks(self) -> list[Task]:
        return [task for task in self.tasks.values() if not task.dependencies]

    def executable_tasks(self) -> list[Task]:
        return [task for task in self.tasks.values() if task.is_executable(self.tasks)]

    def compute_execution_order(self) -> bool:
        order: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(task: Task) -> bool:
            if task.id in visiting:
                return False
            if task.id in visited:
                return True
            visiting.add(task.id)
            for dep_id in task.dependencies:
                dep = self.tasks.get(dep_id)
                if dep and not visit(dep):
                    return False
            visiting.remove(task.id)
            visited.add(task.id)
            order.append(task.id)
            return True

        for task in self.tasks.values():
            if task.id not in visited and not visit(task):
                return False
        self._execution_order = order
        return True

    def execution_order(self) -> list[str]:
        if not self._execution_order:
            self.compute_execution_order()
        return list(self._execution_order)

    def execution_batches(self) -> list[list[Task]]:
        remaining = set(self.tasks)
        completed: set[str] = set()
        batches: list[list[Task]] = []
        while remaining:
            batch_ids = [
                task_id
                for task_id in self.execution_order()
                if task_id in remaining
                and all(dep_id in completed for dep_id in self.tasks[task_id].dependencies)
            ]
            if not batch_ids:
                break
            batch = [self.tasks[task_id] for task_id in batch_ids]
            batches.append(batch)
            completed.update(batch_ids)
            remaining.difference_update(batch_ids)
        return batches

    def progress(self) -> float:
        if not self.tasks:
            return 1.0
        completed = sum(1 for task in self.tasks.values() if task.status == TaskStatus.COMPLETED)
        return completed / len(self.tasks)

    def is_all_completed(self) -> bool:
        return all(task.status == TaskStatus.COMPLETED for task in self.tasks.values())

    def has_failed(self) -> bool:
        return any(
            task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} for task in self.tasks.values()
        )

    def mark_started(self) -> None:
        self.status = PlanStatus.RUNNING
        self.start_time = time.time()

    def mark_completed(self) -> None:
        self.status = PlanStatus.COMPLETED
        self.end_time = time.time()

    def mark_failed(self) -> None:
        self.status = PlanStatus.FAILED
        self.end_time = time.time()

    def summarize(self) -> str:
        batches = self.execution_batches()
        first_batch = ", ".join(task.id for task in batches[0]) if batches else "none"
        final_batch = ", ".join(task.id for task in batches[-1]) if batches else "none"
        return (
            f"Plan {self.id}: {self.summary or self.goal}\n"
            f"Tasks: {len(self.tasks)} | Parallel batches: {len(batches)} | "
            f"Executable now: {len(self.executable_tasks())}\n"
            f"First batch: {first_batch}\n"
            f"Final convergence: {final_batch}"
        )

    def snapshot(self, reason: str) -> PlanVersion:
        return PlanVersion(
            version=self.version,
            plan_id=self.id,
            status=self.status,
            summary=self.summary,
            reason=reason,
            tasks=[Task.from_dict(task.to_dict()) for task in self.all_tasks()],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "goal": self.goal,
            "version": self.version,
            "replan_count": self.replan_count,
            "status": self.status.value,
            "summary": self.summary,
            "created_at": self.created_at,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "tasks": [task.to_dict() for task in self.all_tasks()],
            "history": [revision.to_dict() for revision in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionPlan:
        schema_version = int(data.get("schema_version") or 0)
        if schema_version not in {1, PLAN_SCHEMA_VERSION}:
            raise ValueError(f"unsupported plan schema version: {schema_version}")
        plan = cls(
            id=str(data["id"]),
            goal=str(data.get("goal") or ""),
            status=PlanStatus(str(data.get("status") or PlanStatus.CREATED.value)),
            schema_version=PLAN_SCHEMA_VERSION,
            version=int(data.get("version") or 1),
            replan_count=int(data.get("replan_count") or 0),
            summary=str(data.get("summary") or ""),
            created_at=float(data.get("created_at") or time.time()),
            start_time=float(data.get("start_time") or 0.0),
            end_time=float(data.get("end_time") or 0.0),
        )
        raw_tasks = data.get("tasks")
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if isinstance(item, dict):
                    task = Task.from_dict(item)
                    if schema_version == 1 and task.status == TaskStatus.RUNNING:
                        # Schema v1 executed the active task inside the Parent Run. There is no
                        # durable Child identity to recover, so restart that in-flight logical
                        # attempt under the v2 Child-Run contract. Persisted completed work is
                        # preserved unchanged.
                        task.status = TaskStatus.PENDING
                        task.attempt = max(0, task.attempt - 1)
                        task.start_time = 0.0
                        task.end_time = 0.0
                        task.result = ""
                        task.error = ""
                    plan.add_task(task)
        raw_history = data.get("history")
        if isinstance(raw_history, list):
            plan.history = [
                PlanVersion.from_dict(item) for item in raw_history if isinstance(item, dict)
            ]
        if not plan.compute_execution_order():
            raise ValueError("persisted plan contains a cyclic dependency")
        return plan


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None

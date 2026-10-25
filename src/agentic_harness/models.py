"""Small contracts shared by agents, benchmarks, and the execution driver."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agentic_harness.execution import Container


@dataclass(frozen=True)
class Task:
    task_id: str
    instructions: str
    docker_image: str
    artifacts: tuple[str, ...]
    files_only: bool = False


@dataclass
class AgentResult:
    status: str
    submission: str = ""
    model_calls: int = 0
    cost: float | None = None
    messages: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Grade:
    task_id: str
    passed: bool
    score: float
    details: dict


@dataclass
class TaskResult:
    task_id: str
    execution_status: str = "error"
    agent_status: str = "not_started"
    passed: bool | None = None
    score: float | None = None
    duration_seconds: float = 0.0
    diagnostics: dict = field(default_factory=dict)


@dataclass
class RunResult:
    results: list[TaskResult]
    summary: dict
    duration_seconds: float


def summarize_results(results: list[TaskResult], total: int) -> dict:
    """Summarize available results against all selected tasks, including pending ones."""
    graded = sum(result.passed is not None for result in results)
    passed = sum(result.passed is True for result in results)
    return {
        "total": total,
        "completed": sum(result.execution_status == "completed" for result in results),
        "graded": graded,
        "passed": passed,
        "failed": sum(result.passed is False for result in results),
        "errors": sum(result.execution_status == "error" for result in results),
        "timed_out": sum(result.execution_status == "timed_out" for result in results),
        "interrupted": sum(result.execution_status == "interrupted" for result in results),
        "accuracy": passed / total if total else 0.0,
        "graded_accuracy": passed / graded if graded else None,
    }


class Agent(Protocol):
    network: str
    secrets: tuple[str, ...]

    def image_for(self, task: Task) -> str: ...
    def run(self, container: "Container", task: Task) -> AgentResult: ...


class Benchmark(Protocol):
    grader_image: str

    def task_ids(self) -> list[str]: ...
    def load_task(self, task_id: str) -> Task: ...
    def prepare(self, container: "Container", task: Task) -> str: ...
    def evaluate(self, container: "Container", task: Task) -> Grade: ...

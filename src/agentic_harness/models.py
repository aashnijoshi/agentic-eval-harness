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


class Agent(Protocol):
    image: str
    base_image: str
    network: str
    secrets: tuple[str, ...]

    def run(self, container: "Container", task: Task) -> AgentResult: ...


class Benchmark(Protocol):
    grader_image: str

    def load_task(self, task_id: str) -> Task: ...
    def prepare(self, container: "Container", task: Task) -> str: ...
    def evaluate(self, container: "Container", task: Task) -> Grade: ...

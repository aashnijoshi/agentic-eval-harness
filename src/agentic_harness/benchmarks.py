"""Adapter for the existing bash benchmark; its scripts remain authoritative."""

import json
import math
from pathlib import Path, PurePosixPath

from agentic_harness.models import Grade, Task

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BashOperations:
    grader_image = (
        "python:3.11-slim@sha256:"
        "b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377"
    )

    def __init__(self, directory: Path | None = None):
        self.directory = directory or PROJECT_ROOT / "benchmarks/bash-operations"

    def load_task(self, task_id: str) -> Task:
        if task_id != "bash-001":
            raise ValueError("This implementation step supports only bash-001")
        tasks = json.loads((self.directory / "dataset.json").read_text())
        data = next((task for task in tasks if task["task_id"] == task_id), None)
        if data is None:
            raise ValueError(f"Task {task_id} not found in dataset")
        artifact = PurePosixPath(data["verification"]["file"])
        if artifact.is_absolute() or ".." in artifact.parts or str(artifact) == ".":
            raise ValueError("Artifact must be a relative workspace path")
        return Task(task_id, data["problem_statement"], data["docker_image"], (str(artifact),))

    def prepare(self, container, task: Task) -> str:
        # These files are copied only into the separate trusted container.
        container.execute(["mkdir", "-p", "/trusted"])
        for name in ("setup.py", "evaluate.py", "dataset.json"):
            container.copy_in(self.directory / name, f"/trusted/{name}")
        result = container.execute([
            "python", "-B", "/trusted/setup.py",
            "--workspace-dir", "/workspace", "--task", task.task_id,
        ])
        return result.stdout

    def evaluate(self, container, task: Task) -> Grade:
        result = container.execute([
            "python", "-B", "/trusted/evaluate.py",
            "--workspace-dir", "/submission", "--task", task.task_id,
        ], check=False)
        data = json.loads(result.stdout)
        score = data.get("score")
        if (
            data.get("task_id") != task.task_id
            or type(data.get("passed")) is not bool
            or type(score) not in (int, float)
            or not math.isfinite(score)
            or score != float(data["passed"])
            or not isinstance(data.get("details"), dict)
            or result.returncode != (0 if data["passed"] else 1)
        ):
            raise ValueError("Invalid result from bash-operations grader")
        return Grade(task.task_id, data["passed"], float(score), data["details"])

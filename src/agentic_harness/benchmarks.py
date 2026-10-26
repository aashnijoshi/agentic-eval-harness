"""Two concrete adapters; the supplied benchmark scripts define their scores."""

import json
import math
from pathlib import Path, PurePosixPath

from agentic_harness.models import Grade, Task

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def workspace_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Workspace paths must be nonempty strings")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError(f"Path must stay inside the workspace: {value!r}")
    return str(path)


def read_tasks(directory: Path, id_field: str) -> dict:
    rows = json.loads((directory / "dataset.json").read_text())
    tasks = {}
    if not isinstance(rows, list) or not rows:
        raise ValueError("Dataset must be a nonempty list")
    for row in rows:
        task_id = row.get(id_field) if isinstance(row, dict) else None
        if not isinstance(task_id, str) or not task_id or task_id in tasks:
            raise ValueError(f"Invalid or duplicate task ID: {task_id!r}")
        tasks[task_id] = row
    return tasks


def parse_grade(process, task: Task, *, failed_exit_code: int) -> Grade:
    data = json.loads(process.stdout)
    if not isinstance(data, dict):
        raise ValueError("Grader result must be a JSON object")
    score = data.get("score")
    if (
        data.get("task_id") != task.task_id
        or type(data.get("passed")) is not bool
        or type(score) not in (int, float)
        or not math.isfinite(score)
        or score != float(data["passed"])
        or not isinstance(data.get("details"), dict)
        or process.returncode != (0 if data["passed"] else failed_exit_code)
    ):
        raise ValueError("Invalid result from benchmark grader")
    return Grade(task.task_id, data["passed"], float(score), data["details"])


class BashOperations:
    grader_image = (
        "python@sha256:"
        "b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377"
    )

    def __init__(self, directory: Path | None = None):
        self.directory = directory or PROJECT_ROOT / "benchmarks/bash-operations"
        self.tasks = read_tasks(self.directory, "task_id")

    def task_ids(self) -> list[str]:
        return list(self.tasks)

    def load_task(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise ValueError(f"Task {task_id!r} not found in bash-operations")
        data = self.tasks[task_id]
        verification = data["verification"]
        if verification["type"] == "file_exists":
            paths = verification["files"]
        elif verification["type"] in ("file_content", "file_content_contains"):
            paths = [verification["file"]]
        else:
            raise ValueError(f"Unsupported verification type: {verification['type']}")
        artifacts = tuple(dict.fromkeys(workspace_path(path) for path in paths))
        for path in data.get("setup", {}).get("files", {}):
            workspace_path(path)
        return Task(task_id, data["problem_statement"], data["docker_image"], artifacts)

    def prepare(self, container, task: Task) -> str:
        # Keep benchmark files in the trusted container
        container.execute(["mkdir", "-p", "/trusted"])
        for name in ("setup.py", "evaluate.py", "dataset.json"):
            container.copy_in(self.directory / name, f"/trusted/{name}")
        return container.execute([
            "python", "-B", "/trusted/setup.py",
            "--workspace-dir", "/workspace", "--task", task.task_id,
        ]).stdout

    def evaluate(self, container, task: Task) -> Grade:
        process = container.execute([
            "python", "-B", "/trusted/evaluate.py",
            "--workspace-dir", "/submission", "--task", task.task_id,
        ], check=False)
        return parse_grade(process, task, failed_exit_code=1)


class PythonTasks:
    # Use the Python runtime for locked pytest in a fresh grader container
    grader_image = "harness-mini:python"

    def __init__(self, directory: Path | None = None):
        self.directory = directory or PROJECT_ROOT / "benchmarks/python-tasks"
        self.tasks = read_tasks(self.directory, "id")

    def task_ids(self) -> list[str]:
        return list(self.tasks)

    def load_task(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise ValueError(f"Task {task_id!r} not found in python-tasks")
        data = self.tasks[task_id]
        artifact = workspace_path(data.get("solution_file", "solution.py"))
        if artifact == "test_solution.py":
            raise ValueError("Solution path must not replace the trusted test file")
        return Task(task_id, data["prompt"], data["docker_image"], (artifact,), files_only=True)

    def prepare(self, container, task: Task) -> str:
        solution = task.artifacts[0]
        parent = PurePosixPath(solution).parent
        container.execute([
            "mkdir", "-p", "/trusted", f"/prepared/{parent}", f"/workspace/{parent}",
        ])
        for name in ("prepare.py", "grade.py", "dataset.json", "requirements.txt"):
            container.copy_in(self.directory / name, f"/trusted/{name}")
        result = container.execute([
            "python", "-B", "/trusted/prepare.py", task.task_id,
            "--workspace", "/prepared", "--skip-install",
        ])
        # Keep tests here and give the agent only the solution stub
        container.execute(["cp", f"/prepared/{solution}", f"/workspace/{solution}"])
        return result.stdout

    def evaluate(self, container, task: Task) -> Grade:
        container.execute(["cp", "/prepared/test_solution.py", "/submission/test_solution.py"])
        process = container.execute([
            "python", "-B", "/trusted/grade.py", task.task_id,
            "--workspace", "/submission",
        ], check=False, timeout=40)
        # A zero exit status can still mean a failed grade
        return parse_grade(process, task, failed_exit_code=0)

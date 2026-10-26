"""Run the pinned mini agent inside an existing task container."""

import json
import math
import os
import re
from pathlib import Path

from agentic_harness.models import AgentResult, Task


class MiniSweAgent:
    runtimes = {
        "alpine:latest": "harness-mini:alpine",
        "python:3.11-slim": "harness-mini:python",
    }

    def __init__(
        self, *, model: str | None = None, replay: Path | None = None,
        api_key_env: str = "OPENAI_API_KEY", timeout: float = 60,
        step_limit: int = 20, cost_limit: float = 1.0,
    ):
        if (model is None) == (replay is None):
            raise ValueError("Choose exactly one of --model or --replay")
        if (
            not math.isfinite(timeout) or not math.isfinite(cost_limit)
            or timeout <= 0 or step_limit <= 0 or cost_limit <= 0
        ):
            raise ValueError("Timeout, step limit, and cost limit must be finite and positive")
        self.responses = None
        self.env_names = ()
        self.secrets = ()
        if replay is not None:
            self.responses = json.loads(replay.read_text())
            if isinstance(self.responses, dict):
                if not self.responses or not all(isinstance(key, str) and key for key in self.responses):
                    raise ValueError("Replay mapping must have nonempty task ID keys")
                batches = self.responses.values()
            else:
                batches = [self.responses]
            if not all(
                isinstance(batch, list) and batch and all(isinstance(item, str) for item in batch)
                for batch in batches
            ):
                raise ValueError("Replay must be a response list or a task-ID-to-response-list mapping")
        else:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", api_key_env):
                raise ValueError("Invalid API key environment variable name")
            key = os.environ.get(api_key_env)
            if not key:
                raise ValueError(f"Set {api_key_env} before requesting a live model run")
            self.env_names = (api_key_env,)
            self.secrets = (key,)
        self.model = model
        self.timeout = timeout
        self.step_limit = step_limit
        self.cost_limit = cost_limit
        self.network = "none" if replay is not None else "bridge"

    def image_for(self, task: Task) -> str:
        try:
            return self.runtimes[task.docker_image]
        except KeyError as error:
            raise ValueError(f"No mini runtime for declared image {task.docker_image}") from error

    def run(self, container, task: Task) -> AgentResult:
        responses = self.responses
        if isinstance(responses, dict):
            if task.task_id not in responses:
                raise ValueError(f"No replay responses for task {task.task_id}")
            responses = responses[task.task_id]
        container.copy_in(Path(__file__).with_name("mini_runner.py"), "/mini_runner.py")
        instructions = task.instructions
        # Match complete paths, not a suffix of another filename or directory.
        missing_paths = [
            path for path in task.artifacts
            if not re.search(rf"(?<![\w./-]){re.escape(path)}(?![\w./-])", instructions)
        ]
        if missing_paths:
            instructions += "\n\nRequired submission artifact paths, relative to /workspace: "
            instructions += ", ".join(f"`{path}`" for path in missing_paths) + "."
        request = {
            "task": instructions, "model": self.model, "responses": responses,
            "step_limit": self.step_limit, "cost_limit": self.cost_limit,
        }
        process = container.execute(
            ["python", "-B", "/mini_runner.py"],
            input=json.dumps(request), env_names=self.env_names, timeout=self.timeout,
        )
        data = json.loads(process.stdout)
        return AgentResult(**data)

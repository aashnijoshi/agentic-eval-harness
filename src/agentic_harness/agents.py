"""Run the pinned mini agent inside an existing task container."""

import json
import math
import os
import re
from pathlib import Path

from agentic_harness.models import AgentResult, Task


class MiniSweAgent:
    image = "harness-mini:alpine"
    base_image = "alpine:latest"

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
            if not isinstance(self.responses, list) or not self.responses or not all(
                isinstance(item, str) for item in self.responses
            ):
                raise ValueError("Replay file must contain a nonempty JSON list of responses")
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

    def run(self, container, task: Task) -> AgentResult:
        container.copy_in(Path(__file__).with_name("mini_runner.py"), "/mini_runner.py")
        request = {
            "task": task.instructions, "model": self.model, "responses": self.responses,
            "step_limit": self.step_limit, "cost_limit": self.cost_limit,
        }
        process = container.execute(
            ["python", "-B", "/mini_runner.py"],
            input=json.dumps(request), env_names=self.env_names, timeout=self.timeout,
        )
        data = json.loads(process.stdout)
        return AgentResult(**data)

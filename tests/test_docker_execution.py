"""Opt-in tests using real Docker containers and mini's deterministic model."""

import json
import os
import subprocess

import pytest

from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PROJECT_ROOT
from agentic_harness.execution import run_task

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1",
    reason="Set RUN_DOCKER_TESTS=1 to run offline Docker integration tests",
)


def assert_clean(result):
    containers = subprocess.run([
        "docker", "ps", "-aq", "--filter",
        f"label=agentic-harness.execution={result.diagnostics['execution_id']}",
    ], capture_output=True, text=True, check=True, timeout=15)
    assert containers.stdout.strip() == ""
    assert not result.diagnostics.get("cleanup_errors")


def test_real_mini_passes_bash_001():
    agent = MiniSweAgent(replay=PROJECT_ROOT / "tests/fixtures/bash_001_responses.json")
    result = run_task(BashOperations(), agent, "bash-001")
    assert_clean(result)
    assert result.execution_status == "completed", result.diagnostics
    assert result.agent_status == "Submitted"
    assert result.passed is True and result.score == 1.0
    assert result.duration_seconds > 0
    assert result.diagnostics["artifacts"]["hello.txt"]["preview"] == "Hello World\n"
    assert result.diagnostics["agent"]["model_calls"] == 3
    assert result.diagnostics["agent"]["cost"] == 0.0
    prompt = result.diagnostics["agent"]["messages"][1]["content"]
    assert BashOperations().load_task("bash-001").instructions in prompt


def test_agent_cannot_replace_trusted_grader(tmp_path):
    replay = tmp_path / "responses.json"
    replay.write_text(json.dumps([
        "```bash\ntest ! -e /trusted && mkdir /trusted && "
        "printf 'print(1)' > /trusted/evaluate.py && printf wrong > hello.txt\n```",
        "```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```",
    ]))
    result = run_task(BashOperations(), MiniSweAgent(replay=replay), "bash-001")
    assert_clean(result)
    assert result.execution_status == "completed", result.diagnostics
    assert result.agent_status == "Submitted"
    assert result.passed is False and result.score == 0.0
    assert result.diagnostics["grading"] == {"expected": "Hello World", "actual": "wrong"}


def test_agent_timeout_cleans_containers(tmp_path):
    replay = tmp_path / "responses.json"
    replay.write_text(json.dumps(["```bash\nsleep 30\n```"] ))
    result = run_task(BashOperations(), MiniSweAgent(replay=replay, timeout=2), "bash-001")
    assert_clean(result)
    assert result.execution_status == "timed_out", result.diagnostics
    assert result.agent_status == "timed_out"
    assert result.passed is None and result.score is None


def test_symlink_submission_is_rejected(tmp_path):
    replay = tmp_path / "responses.json"
    replay.write_text(json.dumps([
        "```bash\nln -s /etc/passwd hello.txt\n```",
        "```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```",
    ]))
    result = run_task(BashOperations(), MiniSweAgent(replay=replay), "bash-001")
    assert_clean(result)
    assert result.execution_status == "error", result.diagnostics
    assert result.diagnostics["phase"] == "artifacts"
    assert "Unsafe artifact" in result.diagnostics["error"]
    assert result.passed is None and result.score is None

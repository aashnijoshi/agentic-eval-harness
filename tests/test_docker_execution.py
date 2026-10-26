"""Opt-in tests using real Docker containers and mini's deterministic model."""

import json
import os
from pathlib import PurePosixPath
import shlex
import shutil
import subprocess

import pytest

from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PythonTasks, PROJECT_ROOT
from agentic_harness.execution import Container, run_benchmark, run_task

RESPONSES = PROJECT_ROOT / "tests/fixtures/task_responses.json"
CASES = [(kind, task_id) for kind in (BashOperations, PythonTasks) for task_id in kind().task_ids()]

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


@pytest.mark.parametrize("benchmark_type,task_id", [
    (BashOperations, "bash-001"), (PythonTasks, "py-001"),
])
def test_live_provider_error_is_structured_and_redacted(benchmark_type, task_id, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-dummy-key")
    execute = Container.execute
    bootstrap = """
import importlib
import os
import runpy
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
from litellm.llms.openai.common_utils import OpenAIError
provider = importlib.import_module("litellm.main").openai_chat_completions
error = OpenAIError(status_code=401, message="Offline mocked rejection: offline-dummy-key")
with patch.object(provider, "completion", side_effect=error) as mocked:
    runpy.run_path("/mini_runner.py", run_name="__main__")
    assert mocked.call_count == 1
"""

    def execute_with_mocked_provider(container, arguments, **kwargs):
        if arguments == ["python", "-B", "/mini_runner.py"]:
            arguments = ["python", "-B", "-c", bootstrap]
        return execute(container, arguments, **kwargs)

    monkeypatch.setattr(Container, "execute", execute_with_mocked_provider)
    agent = MiniSweAgent(model="openai/gpt-5-mini")
    # Even if the mock breaks, this container cannot contact a paid provider.
    agent.network = "none"
    result = run_task(benchmark_type(), agent, task_id)
    assert_clean(result)
    assert result.execution_status == result.agent_status == "error"
    assert result.diagnostics["phase"] == "agent"
    assert result.passed is None and result.score is None
    assert "grading" not in result.diagnostics
    outcome = result.diagnostics["agent"]
    assert outcome["error"].startswith("AuthenticationError:")
    assert "Offline mocked rejection: [REDACTED]" in outcome["error"]
    assert outcome["model_calls"] == 0 and outcome["cost"] == 0.0
    assert "JSONDecodeError" not in result.diagnostics["error"]
    assert "offline-dummy-key" not in json.dumps(result.diagnostics)


@pytest.mark.parametrize("benchmark_type,task_id", CASES, ids=[task_id for _, task_id in CASES])
def test_real_mini_passes_every_task(benchmark_type, task_id):
    benchmark = benchmark_type()
    task = benchmark.load_task(task_id)
    result = run_task(benchmark, MiniSweAgent(replay=RESPONSES), task_id)
    assert_clean(result)
    assert result.execution_status == "completed", result.diagnostics
    assert result.agent_status == "Submitted"
    assert result.passed is True and result.score == 1.0
    assert result.duration_seconds > 0
    assert set(result.diagnostics["artifacts"]) == set(task.artifacts)
    assert result.diagnostics["agent"]["model_calls"] == len(json.loads(RESPONSES.read_text())[task_id])
    assert result.diagnostics["agent"]["cost"] == 0.0
    prompt = result.diagnostics["agent"]["messages"][1]["content"]
    assert task.instructions in prompt
    assert "test_solution.py" not in result.diagnostics["artifacts"]


@pytest.mark.parametrize("benchmark_type,task_id", CASES, ids=[task_id for _, task_id in CASES])
def test_incorrect_submissions_fail_every_task(benchmark_type, task_id, tmp_path):
    benchmark = benchmark_type()
    task = benchmark.load_task(task_id)
    path = task.artifacts[0]
    command = f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))} && printf wrong > {shlex.quote(path)}"
    replay = tmp_path / "wrong.json"
    replay.write_text(json.dumps([
        f"```bash\n{command}\n```",
        "```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```",
    ]))
    result = run_task(benchmark, MiniSweAgent(replay=replay), task_id)
    assert_clean(result)
    assert result.execution_status == "completed", result.diagnostics
    assert result.agent_status == "Submitted"
    assert result.passed is False and result.score == 0.0


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


def test_python_tests_and_pytest_configuration_are_not_agent_controlled(tmp_path):
    replay = tmp_path / "tamper.json"
    replay.write_text(json.dumps([
        "```bash\ntest ! -e /prepared && test ! -e /trusted && test ! -e test_solution.py && "
        "printf 'def add(): return 0\\n' > solution.py && "
        "printf 'def test_fake(): assert True\\n' > test_solution.py && "
        "printf 'def pytest_sessionfinish(session): session.exitstatus = 0\\n' > conftest.py && "
        "printf 'raise SystemExit(0)\\n' > pytest.py && "
        "printf '[pytest]\\naddopts = --collect-only\\n' > pytest.ini\n```",
        "```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```",
    ]))
    result = run_task(PythonTasks(), MiniSweAgent(replay=replay), "py-001")
    assert_clean(result)
    assert result.execution_status == "completed", result.diagnostics
    assert result.passed is False and result.score == 0.0
    assert set(result.diagnostics["artifacts"]) == {"solution.py"}
    assert result.diagnostics["grading"]["return_code"] == 1
    assert "test_add" in result.diagnostics["grading"]["stdout"]


def test_python_solution_must_be_a_file_not_a_package(tmp_path):
    replay = tmp_path / "directory.json"
    replay.write_text(json.dumps([
        "```bash\nrm solution.py && mkdir solution.py && touch solution.py/conftest.py\n```",
        "```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```",
    ]))
    result = run_task(PythonTasks(), MiniSweAgent(replay=replay), "py-001")
    assert_clean(result)
    assert result.execution_status == "error", result.diagnostics
    assert result.diagnostics["phase"] == "artifacts"
    assert "regular file" in result.diagnostics["error"]


@pytest.mark.parametrize("benchmark_type,grader", [(BashOperations, "evaluate.py"), (PythonTasks, "grade.py")])
def test_malformed_grader_json_is_an_execution_error(benchmark_type, grader, tmp_path):
    shutil.copytree(benchmark_type().directory, tmp_path / "benchmark")
    (tmp_path / "benchmark" / grader).write_text("print('not-json')\n")
    benchmark = benchmark_type(tmp_path / "benchmark")
    result = run_task(benchmark, MiniSweAgent(replay=RESPONSES), benchmark.task_ids()[0])
    assert_clean(result)
    assert result.execution_status == "error", result.diagnostics
    assert result.diagnostics["phase"] == "grading"
    assert result.passed is None and result.score is None


def test_python_run_isolates_workspace_and_container_state(tmp_path):
    responses = json.loads(RESPONSES.read_text())
    responses["py-001"][0] = responses["py-001"][0].replace(
        "```bash\n", "```bash\ntouch leaked.txt /tmp/previous-task && ", 1,
    )
    responses["py-002"][0] = responses["py-002"][0].replace(
        "```bash\n", "```bash\ntest ! -e leaked.txt && test ! -e /tmp/previous-task && "
        "test \"$(cat solution.py)\" = '# Write your solution here' && ", 1,
    )
    replay = tmp_path / "isolated.json"
    replay.write_text(json.dumps(responses))
    result = run_benchmark(PythonTasks(), MiniSweAgent(replay=replay), ["py-001", "py-002"])
    for task in result.results:
        assert_clean(task)
        assert task.passed is True, task.diagnostics
    assert result.summary["passed"] == 2 and result.summary["accuracy"] == 1.0
    assert len({task.diagnostics["execution_id"] for task in result.results}) == 2


def test_multi_task_run_continues_after_agent_error(tmp_path):
    responses = json.loads(RESPONSES.read_text())
    del responses["py-001"]
    replay = tmp_path / "missing-response.json"
    replay.write_text(json.dumps(responses))
    result = run_benchmark(PythonTasks(), MiniSweAgent(replay=replay), ["py-001", "py-002"])
    for task in result.results:
        assert_clean(task)
    assert result.results[0].execution_status == "error"
    assert result.results[1].passed is True, result.results[1].diagnostics
    assert result.summary["errors"] == 1 and result.summary["passed"] == 1
    assert result.summary["accuracy"] == 0.5 and result.summary["graded_accuracy"] == 1.0


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

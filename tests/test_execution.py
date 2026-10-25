import io
import json
import shutil
import subprocess
import tarfile
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from agentic_harness import cli, execution
from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PROJECT_ROOT
from agentic_harness.models import Task, TaskResult

RESPONSES = PROJECT_ROOT / "tests/fixtures/bash_001_responses.json"


def test_task_instructions_come_from_dataset(tmp_path):
    directory = tmp_path / "benchmark"
    shutil.copytree(PROJECT_ROOT / "benchmarks/bash-operations", directory)
    dataset = json.loads((directory / "dataset.json").read_text())
    dataset[0]["problem_statement"] = "A changed instruction from the dataset"
    (directory / "dataset.json").write_text(json.dumps(dataset))
    task = BashOperations(directory).load_task("bash-001")
    assert task.instructions == dataset[0]["problem_statement"]
    with pytest.raises(ValueError, match="only bash-001"):
        BashOperations(directory).load_task("bash-002")


def test_mini_receives_public_task_and_replay_without_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    calls = {}

    def execute(arguments, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, '{"status":"Submitted"}', "")

    container = SimpleNamespace(copy_in=lambda *_: None, execute=execute)
    task = BashOperations().load_task("bash-001")
    agent = MiniSweAgent(replay=RESPONSES)
    assert agent.run(container, task).status == "Submitted"
    assert json.loads(calls["input"])["task"] == task.instructions
    assert "verification" not in json.loads(calls["input"])
    assert calls["env_names"] == ()
    assert agent.network == "none"
    assert agent.secrets == ()


def test_live_run_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="exactly one"):
        MiniSweAgent()
    with pytest.raises(ValueError, match="Set OPENAI_API_KEY"):
        MiniSweAgent(model="example/model")


@pytest.mark.parametrize("limits", [{"timeout": float("nan")}, {"cost_limit": float("inf")}])
def test_reject_nonfinite_limits(limits):
    with pytest.raises(ValueError, match="finite and positive"):
        MiniSweAgent(replay=RESPONSES, **limits)


@pytest.mark.parametrize("changes,returncode", [
    ({"task_id": "other"}, 1),
    ({"passed": "false"}, 1),
    ({"score": float("nan")}, 1),
    ({"score": 1.0}, 1),
    ({}, 0),
])
def test_reject_invalid_grades(changes, returncode):
    data = {"task_id": "bash-001", "passed": False, "score": 0.0, "details": {}}
    data.update(changes)
    container = SimpleNamespace(execute=lambda *_, **__: subprocess.CompletedProcess(
        [], returncode, json.dumps(data), "",
    ))
    with pytest.raises(ValueError):
        BashOperations().evaluate(container, BashOperations().load_task("bash-001"))


def test_grader_exit_one_is_a_valid_failed_solution():
    data = {"task_id": "bash-001", "passed": False, "score": 0.0,
            "details": {"actual": "wrong", "expected": "Hello World"}}
    container = SimpleNamespace(execute=lambda *_, **__: subprocess.CompletedProcess(
        [], 1, json.dumps(data), "",
    ))
    grade = BashOperations().evaluate(container, BashOperations().load_task("bash-001"))
    assert not grade.passed
    assert grade.score == 0.0
    assert grade.details["actual"] == "wrong"


@pytest.mark.parametrize("name,kind", [
    ("../escaped", tarfile.REGTYPE),
    ("/escaped", tarfile.REGTYPE),
    ("hello.txt", tarfile.SYMTYPE),
    ("hello.txt", tarfile.LNKTYPE),
    ("hello.txt", tarfile.FIFOTYPE),
])
def test_unsafe_artifacts_are_rejected(tmp_path, name, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/etc/passwd" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
        tar.addfile(member)
    with pytest.raises(execution.ExecutionError, match="Unsafe artifact"):
        execution.extract_artifacts(buffer.getvalue(), tmp_path)


def test_cleanup_after_partial_container_start(monkeypatch):
    calls = []

    def docker(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "start":
            raise execution.ExecutionError("start failed")
        return subprocess.CompletedProcess([], 0, b"image-id", b"")

    monkeypatch.setattr(execution, "docker", docker)
    with pytest.raises(execution.ExecutionError, match="start failed"):
        with execution.Container("image", "test", "agent"):
            pytest.fail("Should not enter a container that failed to start")
    assert calls[-1] == ["rm", "--force", "harness-test-agent"]


@pytest.mark.parametrize("failure,status", [
    (RuntimeError("failed secret-value"), "error"),
    (execution.ExecutionTimeout("timed out secret-value"), "timed_out"),
    (KeyboardInterrupt(), "interrupted"),
])
def test_execution_failure_cleans_both_containers(monkeypatch, failure, status):
    closed = []

    class FakeContainer:
        def __init__(self, image, execution_id, role, **kwargs):
            self.image, self.role = image, role
            self.cleanup_errors = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(self.role)

        def copy_in(self, *_):
            pass

        def copy_out(self, *_):
            pass

    class FailingAgent:
        image, base_image, network = "image", "alpine:latest", "none"
        secrets = ("secret-value",)

        def run(self, *_):
            raise failure

    benchmark = SimpleNamespace(
        grader_image="trusted", prepare=lambda *_: "setup",
        load_task=lambda _: Task("bash-001", "from dataset", "alpine:latest", ("hello.txt",)),
    )
    monkeypatch.setattr(execution, "Container", FakeContainer)
    result = execution.run_task(benchmark, FailingAgent(), "bash-001")
    assert result.execution_status == status
    assert result.passed is None and result.score is None
    assert closed == ["agent", "trusted"]
    assert "secret-value" not in json.dumps(asdict(result))


def test_cli_prints_structured_result(monkeypatch):
    result = TaskResult("bash-001", "completed", "Submitted", True, 1.0, 0.25)
    monkeypatch.setattr(cli, "run_task", lambda *_: result)
    output = CliRunner().invoke(cli.cli, [
        "run", "--agent", str(PROJECT_ROOT / "agents/mini-swe-agent"),
        "--benchmark", "bash-operations", "--task_ids", "bash-001",
        "--replay", str(RESPONSES),
    ])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output) == asdict(result)

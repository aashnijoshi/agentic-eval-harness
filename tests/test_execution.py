import io
import json
import runpy
import shutil
import subprocess
import tarfile
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from agentic_harness import cli, execution
from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PythonTasks, PROJECT_ROOT
from agentic_harness.db import Database
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
    with pytest.raises(ValueError, match="not found"):
        BashOperations(directory).load_task("unknown-task")


@pytest.mark.parametrize("benchmark_type,task_id", [
    (kind, task_id) for kind in (BashOperations, PythonTasks) for task_id in kind().task_ids()
])
def test_mini_receives_public_task_and_replay_without_credentials(monkeypatch, benchmark_type, task_id):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    calls = {}

    def execute(arguments, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, '{"status":"Submitted"}', "")

    container = SimpleNamespace(copy_in=lambda *_: None, execute=execute)
    benchmark = benchmark_type()
    task = benchmark.load_task(task_id)
    agent = MiniSweAgent(replay=PROJECT_ROOT / "tests/fixtures/task_responses.json")
    assert agent.run(container, task).status == "Submitted"
    prompt = json.loads(calls["input"])["task"]
    assert prompt.startswith(task.instructions)
    for path in task.artifacts:
        assert path in prompt
    if all(path in task.instructions for path in task.artifacts):
        assert prompt == task.instructions
    else:
        for path in task.artifacts:
            assert prompt.count(path) == 1
    if benchmark_type is PythonTasks:
        assert benchmark.tasks[task_id]["test_code"] not in prompt
    assert "test_solution.py" not in prompt
    assert "test_code" not in prompt and "expected_content" not in prompt
    assert "verification" not in json.loads(calls["input"])
    assert calls["env_names"] == ()
    assert agent.network == "none"
    assert agent.secrets == ()


def test_submission_path_comes_from_metadata_without_exposing_tests(tmp_path):
    dataset = json.loads((PROJECT_ROOT / "benchmarks/python-tasks/dataset.json").read_text())
    dataset[0]["solution_file"] = "answers/custom.py"
    dataset[0]["test_code"] = "PRIVATE_GRADER_SENTINEL"
    (tmp_path / "dataset.json").write_text(json.dumps(dataset))
    task = PythonTasks(tmp_path).load_task(dataset[0]["id"])
    calls = {}

    def execute(arguments, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, '{"status":"Submitted"}', "")

    MiniSweAgent(replay=RESPONSES).run(SimpleNamespace(copy_in=lambda *_: None, execute=execute), task)
    prompt = json.loads(calls["input"])["task"]
    assert prompt.startswith(dataset[0]["prompt"])
    assert "`answers/custom.py`" in prompt
    assert "solution.py" not in prompt
    assert "PRIVATE_GRADER_SENTINEL" not in calls["input"]


@pytest.mark.parametrize("instructions,needs_path", [
    ("Write to 'result.py'.", False),
    ("Write to result.py.", False),
    ("There is an unrelated 'result.py.bak'.", True),
    ("There is an unrelated 'other_result.py'.", True),
    ("There is an unrelated 'nested/result.py'.", True),
])
def test_submission_contract_does_not_duplicate_or_confuse_paths(instructions, needs_path):
    task = Task("custom", instructions, "python:3.11-slim", ("result.py",))
    calls = {}

    def execute(arguments, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, '{"status":"Submitted"}', "")

    MiniSweAgent(replay=RESPONSES).run(SimpleNamespace(copy_in=lambda *_: None, execute=execute), task)
    prompt = json.loads(calls["input"])["task"]
    if needs_path:
        assert "`result.py`" in prompt[len(instructions):]
    else:
        assert prompt == instructions


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


@pytest.mark.parametrize("benchmark,failed_code", [(BashOperations(), 1), (PythonTasks(), 0)])
@pytest.mark.parametrize("changes", [
    {"task_id": "other"}, {"passed": "false"}, {"score": float("nan")}, {"score": 1.0},
])
def test_reject_invalid_grades(benchmark, failed_code, changes):
    task = benchmark.load_task(benchmark.task_ids()[0])
    data = {"task_id": task.task_id, "passed": False, "score": 0.0, "details": {}}
    data.update(changes)
    container = SimpleNamespace(execute=lambda *_, **__: subprocess.CompletedProcess(
        [], failed_code, json.dumps(data), "",
    ))
    with pytest.raises(ValueError):
        benchmark.evaluate(container, task)


@pytest.mark.parametrize("benchmark,failed_code", [(BashOperations(), 1), (PythonTasks(), 0)])
def test_failed_solution_uses_json_and_benchmark_exit_convention(benchmark, failed_code):
    task = benchmark.load_task(benchmark.task_ids()[0])
    data = {"task_id": task.task_id, "passed": False, "score": 0.0,
            "details": {"actual": "wrong", "expected": "Hello World"}}
    container = SimpleNamespace(execute=lambda *_, **__: subprocess.CompletedProcess(
        [], failed_code, json.dumps(data), "",
    ))
    grade = benchmark.evaluate(container, task)
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


def test_copy_in_normalizes_ownership_and_preserves_nested_paths(tmp_path, monkeypatch):
    source = tmp_path / "initial"
    (source / "nested").mkdir(parents=True)
    (source / "nested/input.txt").write_text("seed")
    calls = []
    monkeypatch.setattr(execution, "docker", lambda arguments, **kwargs: calls.append((arguments, kwargs)))
    execution.Container("image", "id", "agent").copy_in(source, "/workspace")
    arguments, kwargs = calls[0]
    assert arguments == ["cp", "-", "harness-id-agent:/workspace"]
    with tarfile.open(fileobj=io.BytesIO(kwargs["input"])) as tar:
        member = tar.getmember("nested/input.txt")
        assert member.uid == member.gid == 0
        assert tar.extractfile(member).read() == b"seed"


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
        network = "none"
        secrets = ("secret-value",)

        def image_for(self, _):
            return "image"

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


def test_cli_prints_persisted_run_id(monkeypatch, tmp_path):
    path = tmp_path / "queue.sqlite3"
    monkeypatch.setattr(execution, "run_task", lambda *_: pytest.fail("Submission must not execute tasks"))
    output = CliRunner().invoke(cli.cli, [
        "run", "--agent", str(PROJECT_ROOT / "agents/mini-swe-agent"),
        "--benchmark", "bash-operations", "--task_ids", "bash-001",
        "--replay", str(RESPONSES), "--db", str(path),
    ])
    assert output.exit_code == 0, output.output
    with Database(path) as db:
        assert db.get_run(output.output.strip())["tasks"][0]["state"] == "queued"


@pytest.mark.parametrize("benchmark", [BashOperations(), PythonTasks()])
@pytest.mark.parametrize("stdout", ["not json", "[]", "null", "{}"])
def test_malformed_grader_responses(benchmark, stdout):
    container = SimpleNamespace(execute=lambda *_, **__: subprocess.CompletedProcess([], 0, stdout, ""))
    with pytest.raises(ValueError):
        benchmark.evaluate(container, benchmark.load_task(benchmark.task_ids()[0]))


@pytest.mark.parametrize("benchmark", [BashOperations(), PythonTasks()])
def test_nonzero_grader_error_is_not_a_failed_grade(benchmark):
    task = benchmark.load_task(benchmark.task_ids()[0])
    data = {"task_id": task.task_id, "passed": False, "score": 0.0, "details": {}}
    container = SimpleNamespace(execute=lambda *_, **__: subprocess.CompletedProcess([], 255, json.dumps(data), ""))
    with pytest.raises(ValueError):
        benchmark.evaluate(container, task)


@pytest.mark.parametrize("benchmark_type,id_field,path_field", [
    (BashOperations, "task_id", "verification"), (PythonTasks, "id", "solution_file"),
])
@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../escape", "", ".", "bad\0path"])
def test_invalid_artifact_paths(benchmark_type, id_field, path_field, path, tmp_path):
    original = benchmark_type()
    rows = list(original.tasks.values())
    if path_field == "verification":
        rows[0][path_field]["file"] = path
    else:
        rows[0][path_field] = path
    (tmp_path / "dataset.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        benchmark_type(tmp_path).load_task(rows[0][id_field])


@pytest.mark.parametrize("benchmark_type,id_field", [(BashOperations, "task_id"), (PythonTasks, "id")])
def test_duplicate_dataset_ids_rejected(benchmark_type, id_field, tmp_path):
    rows = list(benchmark_type().tasks.values())
    rows[1][id_field] = rows[0][id_field]
    (tmp_path / "dataset.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="duplicate task ID"):
        benchmark_type(tmp_path)


@pytest.mark.parametrize("install", [True, False])
def test_python_preparation_default_and_skip_install(install, tmp_path, monkeypatch):
    module = runpy.run_path(str(PROJECT_ROOT / "benchmarks/python-tasks/prepare.py"))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    arguments = (tmp_path, "py-001", PROJECT_ROOT / "benchmarks/python-tasks/dataset.json")
    if install:
        module["setup_task"](*arguments)
    else:
        module["setup_task"](*arguments, install_dependencies=False)
    assert bool(calls) == install
    assert (tmp_path / "test_solution.py").read_text() == PythonTasks().tasks["py-001"]["test_code"]
    assert (tmp_path / "solution.py").read_text() == "# Write your solution here\n"


def test_runtime_selection_uses_declared_base_image():
    agent = MiniSweAgent(replay=RESPONSES)
    assert agent.image_for(BashOperations().load_task("bash-002")) == "harness-mini:alpine"
    assert agent.image_for(PythonTasks().load_task("py-001")) == "harness-mini:python"
    with pytest.raises(ValueError, match="No mini runtime"):
        agent.image_for(Task("id", "prompt", "unknown:latest", ()))


@pytest.mark.parametrize("selected", [[], ["bash-001", "unknown"], ["bash-001", "bash-001"], [""]])
def test_invalid_task_selection_fails_before_execution(selected, monkeypatch):
    monkeypatch.setattr(execution, "run_task", lambda *_: pytest.fail("Must validate first"))
    with pytest.raises(ValueError):
        execution.run_benchmark(BashOperations(), MiniSweAgent(replay=RESPONSES), selected)


def test_aggregate_continues_after_grade_and_infrastructure_failures(monkeypatch):
    benchmark = BashOperations()
    tasks = benchmark.task_ids()
    outcomes = [
        TaskResult(tasks[0], "completed", "Submitted", True, 1.0),
        TaskResult(tasks[1], "completed", "Submitted", False, 0.0),
        TaskResult(tasks[2], "error"), TaskResult(tasks[3], "timed_out"),
        TaskResult(tasks[4], "completed", "Submitted", True, 1.0),
    ]
    called = []

    def run_task(benchmark, agent, task_id):
        called.append(task_id)
        return outcomes[tasks.index(task_id)]

    monkeypatch.setattr(execution, "run_task", run_task)
    result = execution.run_benchmark(benchmark, MiniSweAgent(replay=RESPONSES))
    assert called == tasks
    assert result.summary == {
        "total": 5, "completed": 3, "graded": 3, "passed": 2, "failed": 1,
        "errors": 1, "timed_out": 1, "interrupted": 0,
        "accuracy": 2 / 5, "graded_accuracy": 2 / 3,
    }


def test_interrupt_does_not_launch_remaining_tasks(monkeypatch):
    called = []

    def run_task(benchmark, agent, task_id):
        called.append(task_id)
        return TaskResult(task_id, execution_status="interrupted")

    monkeypatch.setattr(execution, "run_task", run_task)
    result = execution.run_benchmark(BashOperations(), MiniSweAgent(replay=RESPONSES))
    assert called == ["bash-001"]
    assert len(result.results) == 5 and result.summary["interrupted"] == 5
    assert all(task.agent_status == "not_started" for task in result.results[1:])


@pytest.mark.parametrize("benchmark_type,name,selection", [
    (BashOperations, "bash-operations", None),
    (BashOperations, "bash-operations", "bash-001, bash-003"),
    (PythonTasks, "python-tasks", None),
    (PythonTasks, "python-tasks", "py-002"),
])
def test_cli_selects_one_many_or_all(benchmark_type, name, selection, monkeypatch, tmp_path):
    path = tmp_path / "queue.sqlite3"
    monkeypatch.setattr(execution, "run_task", lambda *_: pytest.fail("Submission must not execute tasks"))
    arguments = ["run", "--agent", str(PROJECT_ROOT / "agents/mini-swe-agent"),
                 "--benchmark", name, "--replay", str(RESPONSES), "--db", str(path)]
    if selection is not None:
        arguments.extend(["--task_ids", selection])
    output = CliRunner().invoke(cli.cli, arguments)
    assert output.exit_code == 0, output.output
    expected = benchmark_type().task_ids() if selection is None else [item.strip() for item in selection.split(",")]
    with Database(path) as db:
        run = db.get_run(output.output.strip())
    assert run["task_ids"] == expected
    assert all(task["state"] == "queued" for task in run["tasks"])

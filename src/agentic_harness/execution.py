"""One synchronous task lifecycle, with separate agent and trusted containers."""

import hashlib
import io
import subprocess
import tarfile
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, TemporaryFile
from uuid import uuid4

from agentic_harness.models import Agent, Benchmark, RunResult, TaskResult

OUTPUT_LIMIT = 4 * 1024 * 1024
ARTIFACT_LIMIT = 1024 * 1024


class ExecutionError(RuntimeError):
    pass


class ExecutionTimeout(ExecutionError):
    pass


def docker(arguments: list[str], *, input: str | bytes | None = None, timeout=30, check=True):
    # Spool command output to temporary files instead of buffering it in memory.
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        try:
            process = subprocess.run(
                ["docker", *arguments], input=input.encode() if isinstance(input, str) else input,
                stdout=stdout, stderr=stderr, timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ExecutionTimeout(f"docker {arguments[0]} exceeded {timeout}s") from error
        stdout.seek(0)
        stderr.seek(0)
        output, errors = stdout.read(OUTPUT_LIMIT + 1), stderr.read(OUTPUT_LIMIT + 1)
    if len(output) > OUTPUT_LIMIT or len(errors) > OUTPUT_LIMIT:
        raise ExecutionError(f"docker {arguments[0]} output exceeded {OUTPUT_LIMIT} bytes")
    if check and process.returncode:
        raise ExecutionError(
            f"docker {arguments[0]} failed: "
            f"{errors.decode(errors='replace') or output.decode(errors='replace')}"
        )
    return subprocess.CompletedProcess(process.args, process.returncode, output, errors)


def extract_artifacts(archive: bytes, directory: Path) -> None:
    """Copy only ordinary files/directories; never extract untrusted tar paths/links."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = tar.getmembers()
        if len(members) > 100 or sum(member.size for member in members) > ARTIFACT_LIMIT:
            raise ExecutionError("Submission exceeds artifact limits")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ExecutionError(f"Unsafe artifact: {member.name}")
            target = directory.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source:
                    target.write_bytes(source.read())


class Container:
    def __init__(self, image: str, execution_id: str, role: str, *, network="none"):
        self.name = f"harness-{execution_id}-{role}"
        self.image = image
        self.execution_id = execution_id
        self.network = network
        self.cleanup_errors: list[str] = []

    def __enter__(self):
        try:
            # Resolve the tag once so this execution uses a concrete local image ID.
            self.image = docker(["image", "inspect", "--format", "{{.Id}}", self.image]).stdout.decode().strip()
            docker([
                "create", "--name", self.name,
                "--label", f"agentic-harness.execution={self.execution_id}",
                "--network", self.network, "--memory", "512m", "--cpus", "1",
                "--pids-limit", "128", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--workdir", "/workspace",
                self.image, "sleep", "3600",
            ])
            docker(["start", self.name])
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        try:
            result = docker(["rm", "--force", self.name], check=False)
            error = result.stderr.decode(errors="replace")
            if result.returncode and "No such container" not in error:
                self.cleanup_errors.append(error)
        except Exception as error:
            self.cleanup_errors.append(str(error))

    def execute(self, arguments, *, input=None, env_names=(), timeout=30, check=True):
        command = ["exec", "-i", "--workdir", "/workspace"]
        for name in env_names:
            # Forward the named variable, never its value in argv or diagnostics.
            command.extend(["--env", name])
        result = docker([*command, self.name, *arguments], input=input, timeout=timeout, check=check)
        return subprocess.CompletedProcess(
            result.args, result.returncode,
            result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"),
        )

    def copy_in(self, source: Path, target: str):
        # Host UID/GID would make seeded files unwritable under --cap-drop ALL.
        # Normalize ownership in the archive instead of granting extra capabilities.
        def container_owner(member):
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            return member

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            if source.is_dir():
                for child in source.iterdir():
                    tar.add(child, arcname=child.name, filter=container_owner)
                destination = target
            else:
                tar.add(source, arcname=PurePosixPath(target).name, filter=container_owner)
                destination = str(PurePosixPath(target).parent)
        docker(["cp", "-", f"{self.name}:{destination}"], input=archive.getvalue())

    def copy_out(self, source: str, directory: Path, *, missing_ok=False):
        result = docker(["cp", f"{self.name}:{source}", "-"], check=False)
        if result.returncode:
            error = result.stderr.decode(errors="replace")
            if missing_ok and ("Could not find the file" in error or "no such file" in error.lower()):
                return
            raise ExecutionError(f"Artifact transfer failed: {error}")
        extract_artifacts(result.stdout, directory)

    def stop(self):
        # Stop every process before reading the final submission; don't reuse its OS.
        docker(["stop", "--time", "1", self.name])


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


def run_task(benchmark: Benchmark, agent: Agent, task_id: str) -> TaskResult:
    task = benchmark.load_task(task_id)
    image = agent.image_for(task)
    started = time.monotonic()
    execution_id = uuid4().hex
    result = TaskResult(task_id, diagnostics={
        "execution_id": execution_id, "instructions": task.instructions,
        "base_image": task.docker_image,
    })
    trusted = Container(benchmark.grader_image, execution_id, "trusted")
    sandbox = Container(image, execution_id, "agent", network=agent.network)
    phase = "setup"
    try:
        with TemporaryDirectory(prefix="harness-") as temporary:
            initial, artifacts = Path(temporary, "initial"), Path(temporary, "artifacts")
            initial.mkdir()
            artifacts.mkdir()
            with trusted:
                result.diagnostics["setup"] = benchmark.prepare(trusted, task)
                trusted.copy_out("/workspace/.", initial)
                with sandbox:
                    sandbox.copy_in(initial, "/workspace")
                    phase = "agent"
                    outcome = agent.run(sandbox, task)
                    result.agent_status = outcome.status
                    result.diagnostics["agent"] = asdict(outcome)
                    sandbox.stop()
                    if outcome.status == "error":
                        raise ExecutionError(outcome.error or "Agent execution failed")
                    phase = "artifacts"
                    for path in task.artifacts:
                        # docker cp archives a file by basename; preserve its declared parent.
                        destination = artifacts / PurePosixPath(path).parent
                        destination.mkdir(parents=True, exist_ok=True)
                        sandbox.copy_out(f"/workspace/{path}", destination, missing_ok=True)
                        if task.files_only and (artifacts / path).is_dir():
                            raise ExecutionError(f"Solution artifact must be a regular file: {path}")
                entries = list(artifacts.rglob("*"))
                files = [path for path in entries if path.is_file()]
                if len(entries) > 100 or sum(path.stat().st_size for path in files) > ARTIFACT_LIMIT:
                    raise ExecutionError("Submission exceeds artifact limits")
                result.diagnostics["artifacts"] = {
                    str(path.relative_to(artifacts)): {
                        "size_bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "preview": path.read_bytes()[:4096].decode(errors="replace"),
                    }
                    for path in files
                }
                phase = "grading"
                trusted.execute(["mkdir", "-p", "/submission"])
                trusted.copy_in(artifacts, "/submission")
                grade = benchmark.evaluate(trusted, task)
                result.passed, result.score = grade.passed, grade.score
                result.diagnostics["grading"] = grade.details
                result.execution_status = "completed"
    except ExecutionTimeout as error:
        result.execution_status = "timed_out"
        if phase == "agent":
            result.agent_status = "timed_out"
        result.diagnostics["error"] = str(error)
    except KeyboardInterrupt:
        result.execution_status = "interrupted"
        if phase == "agent":
            result.agent_status = "interrupted"
        result.diagnostics["error"] = "Execution interrupted"
    except Exception as error:
        if phase == "agent" and result.agent_status == "not_started":
            result.agent_status = "error"
        result.diagnostics["error"] = f"{type(error).__name__}: {error}"
    result.duration_seconds = round(time.monotonic() - started, 3)
    result.diagnostics.update(phase=phase, images={"agent": sandbox.image, "grader": trusted.image})
    cleanup_errors = trusted.cleanup_errors + sandbox.cleanup_errors
    if cleanup_errors:
        result.diagnostics["cleanup_errors"] = cleanup_errors
        result.execution_status = "error"
    result.diagnostics = redact(result.diagnostics, agent.secrets)
    return result


def run_benchmark(benchmark: Benchmark, agent: Agent, task_ids: list[str] | None = None) -> RunResult:
    """Validate selection first, then execute independent tasks sequentially."""
    selected = benchmark.task_ids() if task_ids is None else task_ids
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("Select at least one task, with no duplicate task IDs")
    for task_id in selected:
        task = benchmark.load_task(task_id)
        agent.image_for(task)
    started = time.monotonic()
    results = []
    interrupted = False
    for task_id in selected:
        if interrupted:
            result = TaskResult(task_id, execution_status="interrupted", diagnostics={
                "error": "Not started because the run was interrupted",
            })
        else:
            result = run_task(benchmark, agent, task_id)
            interrupted = result.execution_status == "interrupted"
        results.append(result)
    graded = sum(result.passed is not None for result in results)
    passed = sum(result.passed is True for result in results)
    summary = {
        "total": len(selected),
        "completed": sum(result.execution_status == "completed" for result in results),
        "graded": graded,
        "passed": passed,
        "failed": sum(result.passed is False for result in results),
        "errors": sum(result.execution_status == "error" for result in results),
        "timed_out": sum(result.execution_status == "timed_out" for result in results),
        "interrupted": sum(result.execution_status == "interrupted" for result in results),
        "accuracy": passed / len(selected),
        "graded_accuracy": passed / graded if graded else None,
    }
    return RunResult(results, summary, round(time.monotonic() - started, 3))

# Agentic Harness Take-Home

A system for running AI agents against benchmark tasks.

## Quick Start

### Installation

Use a Git checkout so `make init` can restore the submodule. If you have the
original starter ZIP without `.git` and with an empty `agents/mini-swe-agent/`,
run these commands once from the project root:

```bash
git init -b main
git clone --depth 1 --branch v1.17.5 https://github.com/SWE-agent/mini-swe-agent.git agents/mini-swe-agent
git submodule add https://github.com/SWE-agent/mini-swe-agent.git agents/mini-swe-agent
git submodule absorbgitdirs
```

The recovered setup pins mini-swe-agent v1.17.5 at
`68d367bf0ccf40ad59a07101a94c21b66e7798ff` through the Git submodule entry.
The ZIP did not preserve the original revision; this compatible v1 release
supports Python 3.11 and the flags used by the examples. Commit the submodule
entry along with the project files when preparing the GitHub submission.

```bash
make init       # Initialize git submodules (mini-swe-agent)
make install    # Install dependencies
source .venv/bin/activate
```

### Docker runtime prerequisite check

These commands build and check the agent runtime only. They do not run a
benchmark, grade a solution, or call a model API. Run them from the project root
with Docker running and the submodule initialized:

```bash
docker build --progress=plain -f docker/mini.Dockerfile -t harness-mini:python .
docker build --progress=plain -f docker/mini.Dockerfile --build-arg BASE_IMAGE=alpine:latest@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a -t harness-mini:alpine .

docker run --rm --network none harness-mini:python mini --help
docker run --rm --network none harness-mini:alpine mini --help
docker run --rm --network none harness-mini:python python /opt/runtime/smoke_test.py
docker run --rm --network none harness-mini:alpine python /opt/runtime/smoke_test.py
```

The default base is a digest-pinned `python:3.11-slim`. The Alpine build uses a
digest-pinned snapshot of the benchmark's `alpine:latest`, adding Python and Bash
through `apk`; it keeps Alpine and musl. Both builds use uv 0.12.5 pinned by digest
and the existing `uv.lock`. The Dockerfile-specific ignore file excludes the
host environment, credentials, Git metadata, harness source, and benchmark data
from the build context.

The smoke test uses mini's own `DefaultAgent`, `LocalEnvironment`, and
`DeterministicModel`: three scripted responses create a file with Bash, check it
with Python, and submit. It also imports the real LiteLLM adapter without calling
it. Networking is disabled, no credentials are injected, and `--rm` removes the
containers after each check. The workspace is temporary container storage.

Verified on Linux ARM64: Python 3.11.17 in the slim runtime and Python 3.14.8 on
Alpine 3.24.3. Both reported `Submitted`, three model calls, and zero cost. These
are scripted model calls, not requests to an API. The Alpine native dependencies
also imported successfully without installing a compiler toolchain.

The base images and Python dependencies are pinned, but Alpine's `apk`
repositories can publish newer system packages. Preserve a built image by its
image ID/digest when an exact runtime snapshot is required. Other CPU
architectures and live model-provider calls have not been tested.

### Execute benchmarks synchronously in Docker

All five `bash-operations` tasks and all three `python-tasks` are supported.
The runtime is selected from each task's declared `docker_image`: Alpine tasks
use `harness-mini:alpine`, and Python tasks use `harness-mini:python`.
These commands use mini's built-in deterministic model and need no API key:

```bash
uv run harness run --agent agents/mini-swe-agent --benchmark bash-operations --task_ids bash-001 --replay tests/fixtures/bash_001_responses.json

# Multiple selected tasks, in the requested order.
uv run harness run --agent agents/mini-swe-agent --benchmark bash-operations --task_ids bash-002,bash-003 --replay tests/fixtures/task_responses.json

# Omit task IDs to execute the entire benchmark sequentially.
uv run harness run --agent agents/mini-swe-agent --benchmark bash-operations --replay tests/fixtures/task_responses.json
uv run harness run --agent agents/mini-swe-agent --benchmark python-tasks --replay tests/fixtures/task_responses.json
```

Every response has the same shape, including single-task runs: `results` is a
list of per-task records, `summary` aggregates those records, and
`duration_seconds` measures the whole run. This replaces the earlier flat
single-task JSON response. Each task retains `task_id`, `execution_status`,
`agent_status`, `passed`, `score`, `duration_seconds`, and diagnostics. Diagnostics
include the dataset-loaded instructions, mini's messages/model-call count,
artifact previews/hashes, grader details, image IDs, and failure phase/error.

`summary.accuracy` is passed / selected tasks, so infrastructure errors do not
silently disappear from the denominator. `graded_accuracy` is passed / tasks
with a valid grade, or null when none were graded. The summary also counts
completed, graded, passed, failed, errored, timed-out, and interrupted tasks.
Scores remain the benchmarks' original binary scores. Unknown, empty, or
duplicate task selections are rejected before any task starts. A failed grade,
agent error, or timeout does not stop later selected tasks. Ctrl-C interrupts
the current task and marks remaining tasks interrupted without starting them.

Replay files may contain a response list (reused independently for each task)
or a mapping from task IDs to response lists. The all-task fixture uses the
mapping form. These are scripted responses to the real mini agent; they do not
bypass its loop or the grader. No solutions or task IDs are embedded in harness
production code. Each fixture task is expected to submit and score 1.0.

Execution proceeds in two separate containers:

1. A fresh trusted Python container receives the benchmark's scripts and dataset.
   Bash uses the digest-pinned Python base. Python grading reuses a separate
   instance of the already-built Python runtime, which supplies locked pytest.
   No package installation or networking is needed during execution.
   Python's preparation script adds a backward-compatible `--skip-install`
   option; default standalone installation behavior is unchanged.
2. Only those initial workspace files and the public task prompt go into an
   isolated mini container using the declared image family. Bash setup files are
   preserved. Python preparation creates tests and a solution stub in the trusted
   container, but copies only the stub to the agent workspace. The grader,
   dataset, and Python tests are never copied into the agent container. Replay
   runs have networking disabled. Each task gets new containers and directories.
   Copy-in archives normalize ownership to container UID/GID 0 so initial files
   remain editable with all container capabilities dropped.
3. After mini finishes, its container is stopped before the declared artifact is
   copied out. Tar paths and file types are checked; links and special files are
   rejected. Nested artifact paths are preserved. Artifacts are limited to 1 MiB
   and 100 entries across the submission. Python accepts only its declared
   solution file, excluding agent-created tests and pytest configuration files.
4. The original evaluator runs against the copied submission in the trusted
   container's clean submission directory. Python tests are copied there from
   trusted preparation immediately before grading. Both benchmarks' JSON and
   exit-code conventions are preserved: Python's grader exits zero even for
   failed solutions. A wrong solution is a completed execution with a failed
   grade, not an infrastructure error. The supplied grader code is unchanged.
5. Context managers remove both containers and host temporary directories on
   success, exceptions, timeouts, and keyboard interruption. No volumes or host
   repository mounts are used. Cleanup errors are reported explicitly.

Exit codes are 0 when all selected tasks pass, 1 when at least one grade fails,
and 2 when any task has an execution error, timeout, or interruption.
Configuration errors also return a nonzero exit code. If a task obtained no
valid grade, its `passed` and `score` are null.

```bash
# Unit/contract tests; Docker is not required.
uv run pytest tests/test_execution.py -q

# Real Docker execution, still entirely offline and free of model API calls.
RUN_DOCKER_TESTS=1 uv run pytest tests/test_docker_execution.py -q

# Both suites; integration tests are skipped unless explicitly enabled.
uv run pytest tests -q
```

A live model path is also available, but has not been tested or called during
development. It requires `--model PROVIDER/MODEL` instead of `--replay` and a
provider key already in the environment. Use `--api-key-env ANTHROPIC_API_KEY`
for an Anthropic key, for example; the default is `OPENAI_API_KEY`. Only the named
key is forwarded, by variable name rather than its value in command arguments.
Known key values are redacted from returned diagnostics. Live runs enable the
agent container's outbound network access. The default limits are 60 seconds,
20 model calls, and an estimated $1 model cost; they can be changed with
`--timeout`, `--step-limit`, and `--cost-limit`. Cost checks happen between calls,
so one call can exceed the configured cost threshold.

This step does not implement persistence, queues, workers, or the
`status`/`results` commands. Images must already be available locally; execution
does not build or pull them. Full logs/artifacts are not retained on disk; the
result contains messages, artifact previews, and grader diagnostics. Docker
daemon failure or an uncatchable process kill can prevent immediate cleanup;
containers carry an `agentic-harness.execution` label for identification.

Known benchmark limitations are preserved: `bash-003` has five logical lines but
no trailing newline (`wc -l` alone returns four), and bash substring/existence
checks remain as supplied. Python grading executes submitted Python code in an
isolated container; withholding test files prevents editing them during agent
execution, but the provided pytest grader is not a hardened checker against
malicious code that manipulates pytest from inside its own Python process.

### Configuring mini-swe-agent

Run the setup wizard to configure your default model and API key:

```bash
mini-extra config setup
```

Read more about how to configure mini-swe-agent: https://mini-swe-agent.com/latest/usage/config/

### Run Examples

```bash
make bash-example
make py-example
```

These commands demonstrate the full workflow: setup → run agent → evaluate.

## Project Structure

```
.
├── benchmarks/
│   ├── bash-operations/      # Bash command benchmark
│   │   ├── dataset.json      # 5 bash tasks
│   │   ├── setup.py          # Setup workspace
│   │   ├── evaluate.py       # Evaluate results
│   │   └── README.md
│   └── python-tasks/         # Python coding benchmark
│       ├── dataset.json      # 3 Python tasks
│       ├── prepare.py        # Setup workspace (different name!)
│       ├── grade.py          # Evaluate results (different name!)
│       └── README.md
├── agents/
│   └── mini-swe-agent/       # Example agent (git submodule)
├── examples/                 # Example scripts
├── src/
│   └── agentic_harness/      # Your harness implementation
└── Makefile
```

## Workflow

Each benchmark follows a three-step process:

1. **Setup**: Prepare workspace with initial files
2. **Execute**: Run agent to solve the task
3. **Evaluate**: Check if task was completed correctly

## Benchmarks

### bash-operations
- 5 simple bash tasks
- Uses `alpine:latest` Docker image
- CLI: `--workspace-dir <path> --task <id>`
- Exit codes: 0 for pass, 1 for fail

[Full documentation](benchmarks/bash-operations/README.md)

### python-tasks
- 3 simple Python tasks
- Uses `python:3.11-slim` Docker image
- CLI: `<task_id> --workspace <path>` (positional argument!)
- Exit code: Always 0 (check JSON for pass/fail)

[Full documentation](benchmarks/python-tasks/README.md)

## mini-swe-agent

Important flags:
- `--exit-immediately`: Required to prevent hanging
- `-y/--yolo`: Skip action confirmations
- `-t/--task`: Task description

For more information, run `mini --help` or read the [mini-swe-agent quickstart](https://mini-swe-agent.com/latest/quickstart/).

## Notes

- Benchmarks have intentionally different interfaces to test flexibility
- Tasks specify `docker_image` field for container execution
- Each benchmark has its own evaluation logic

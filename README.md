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

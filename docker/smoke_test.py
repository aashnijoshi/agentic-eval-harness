"""Exercise mini's agent loop inside a container without a model API."""

import json
import platform
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.test_models import DeterministicModel


def main() -> None:
    # Import the real provider adapter to check its dependencies
    assert LitellmModel is not None
    assert version("mini-swe-agent") == "1.17.5"
    with TemporaryDirectory(prefix="mini-smoke-", dir="/workspace") as workspace:
        model = DeterministicModel(
            outputs=[
                "```bash\nbash -lc 'printf runtime-ok > marker.txt'\n```",
                "```bash\npython -c \"from pathlib import Path; "
                "assert Path('marker.txt').read_text() == 'runtime-ok'; "
                "print('verified')\"\n```",
                "```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```",
            ],
            cost_per_call=0.0,
        )
        agent = DefaultAgent(
            model,
            LocalEnvironment(cwd=workspace, timeout=10),
            step_limit=3,
            cost_limit=0,
        )
        status, _ = agent.run("Create marker.txt containing runtime-ok and verify it.")
        assert status == "Submitted", status
        assert model.n_calls == 3
        assert Path(workspace, "marker.txt").read_text() == "runtime-ok"
        assert "verified" in agent.messages[-3]["content"]
        print(json.dumps({
            "status": status,
            "mini_version": version("mini-swe-agent"),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "model_calls": model.n_calls,
            "cost": model.cost,
        }))


if __name__ == "__main__":
    main()

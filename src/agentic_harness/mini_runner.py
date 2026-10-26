"""Container entry point. No harness installation or interactive setup is needed."""

import json
import sys

import yaml
from minisweagent import __version__
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import builtin_config_dir
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models import get_model
from minisweagent.models.test_models import DeterministicModel


def main() -> None:
    model = agent = None
    result = {"status": "error"}
    try:
        if __version__ != "1.17.5":
            raise RuntimeError(f"Expected mini-swe-agent 1.17.5, got {__version__}")
        request = json.load(sys.stdin)
        config = yaml.safe_load((builtin_config_dir / "default.yaml").read_text())
        if request["responses"] is not None:
            model = DeterministicModel(outputs=request["responses"], cost_per_call=0.0)
        else:
            import litellm

            # Keep provider help banners out of the JSON output
            litellm.suppress_debug_info = True
            model = get_model(request["model"], config.get("model", {}))
        config["agent"].update(
            step_limit=request["step_limit"], cost_limit=request["cost_limit"],
        )
        environment = LocalEnvironment(cwd="/workspace", timeout=10)
        agent = DefaultAgent(model, environment, **config["agent"])
        status, submission = agent.run(request["task"])
        result.update(status=status, submission=submission)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if model is not None:
            result.update(model_calls=model.n_calls, cost=model.cost)
        if agent is not None:
            result["messages"] = agent.messages
        print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()

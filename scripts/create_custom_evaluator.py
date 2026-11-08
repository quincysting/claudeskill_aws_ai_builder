"""Validate, then create, an AgentCore Evaluations custom LLM-as-a-judge evaluator from a JSON config.
Stdlib only; boto3 is imported only to create.

Usage:  python create_custom_evaluator.py --check judge.json [more.json ...] [--level TRACE]   # no AWS
        python create_custom_evaluator.py --create judge.json --name concise_reply [--level TRACE]
        AGENT_MODEL_ID=<the agent's model id> python create_custom_evaluator.py --check judge.json

The config is the CreateEvaluator `evaluatorConfig` body ({"llmAsAJudge": {modelConfig, instructions,
ratingScale}}). The checks catch what the service reports only after submit, or never:
  - the instruction uses no placeholder, or one that isn't valid at the evaluator's level;
  - the name breaks the naming rule seen in the lab (letter first, letters, digits, underscores, 48 max);
  - ratingScale isn't exactly one of numerical, categorical or multiDimensional, or a numerical or
    categorical scale has fewer than 2 distinct values or labels;
  - with AGENT_MODEL_ID set, the judge is from the agent's own model family (self-preference).
A config that uses ground-truth placeholders works in on-demand (batch) evaluations only, never in an
online evaluation config; --check prints a note for those.

Exit codes: 0 all configs passed (or the evaluator was created), 1 a config failed a check, 2 bad arguments,
an unreadable file, a wrongly shaped (malformed) config or a failed AWS call.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Placeholders each level can use (AgentCore developer guide, "Create evaluator").
LEVEL_PLACEHOLDERS = {
    "SESSION": {"context", "available_tools", "actual_tool_trajectory", "expected_tool_trajectory", "assertions"},
    "TRACE": {"context", "assistant_turn", "expected_response"},
    "TOOL_CALL": {"available_tools", "context", "tool_turn", "invoked_skill", "skill_content",
                  "available_skills", "user_message"},
}
GROUND_TRUTH_PLACEHOLDERS = {"expected_response", "assertions", "expected_tool_trajectory"}
SCALE_KINDS = ("numerical", "categorical", "multiDimensional")
MODEL_CONFIG_KINDS = ("bedrockEvaluatorModelConfig", "responsesEvaluatorModelConfig")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")


def model_family(model_id: str) -> str:
    """'us.amazon.nova-2-lite-v1:0' -> 'amazon.nova': drops the cross-Region prefix and the version.

    An inference-profile ARN is reduced to the text after its last '/' first.
    """
    model_id = model_id.rsplit("/", 1)[-1]
    return re.sub(r"^(us|eu|apac|jp|au|ca|us-gov|global)\.", "", model_id).split("-")[0]


def shape_problems(config: Any) -> list[str]:
    """Wrong JSON types that would crash the checks. Missing keys are left to validate()."""
    if not isinstance(config, dict):
        return ["malformed: config must be a JSON object"]
    judge = config.get("llmAsAJudge")
    if judge is None:
        return []
    if not isinstance(judge, dict):
        return ["malformed: llmAsAJudge must be an object"]
    out = []
    if not isinstance(judge.get("instructions", ""), str):
        out.append("malformed: instructions must be a string")
    cfg = judge.get("modelConfig", {})
    if not isinstance(cfg, dict):
        out.append("malformed: modelConfig must be an object")
    else:
        for k in MODEL_CONFIG_KINDS:
            inner = cfg.get(k)
            if k in cfg and (not isinstance(inner, dict) or not isinstance(inner.get("modelId", ""), str)):
                out.append(f"malformed: modelConfig.{k} must be an object with a string modelId")
    scale = judge.get("ratingScale", {})
    if not isinstance(scale, dict):
        return out + ["malformed: ratingScale must be an object"]
    for kind in ("numerical", "categorical"):
        entries = scale.get(kind)
        if kind in scale and (not isinstance(entries, list) or not all(
                isinstance(e, dict) and not isinstance(e.get("value", e.get("label")), (list, dict))
                for e in entries)):
            out.append(f"malformed: ratingScale.{kind} must be a list of objects with scalar values")
    return out


def _placeholders(config: dict[str, Any]) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", config.get("llmAsAJudge", {}).get("instructions", "")))


def _scale_problems(scale: Any) -> list[str]:
    kinds = [k for k in SCALE_KINDS if k in (scale or {})]
    if len(kinds) != 1:
        return [f"ratingScale needs exactly one of {list(SCALE_KINDS)}"]
    kind = kinds[0]
    if kind == "multiDimensional":
        return [] if scale[kind] else ["ratingScale.multiDimensional is empty"]
    key = "value" if kind == "numerical" else "label"
    items = [s.get(key) for s in scale[kind] if isinstance(s, dict)]
    if len(items) < 2 or len(set(items)) != len(items) or None in items:
        return [f"ratingScale.{kind} needs 2+ entries with distinct {key}s"]
    if any(not s.get("definition") for s in scale[kind]):
        return [f"ratingScale.{kind}: give every entry a definition, the judge reads them"]
    return []


def validate(name: str, level: str, config: dict[str, Any], agent_model: str | None = None) -> list[str]:
    """Return problems (empty list = OK)."""
    if shape := shape_problems(config):
        return shape
    problems = []
    if not NAME_RE.match(name):
        problems.append(f"name {name!r}: start with a letter; letters, digits, underscores; max 48")
    if level not in LEVEL_PLACEHOLDERS:
        return problems + [f"level {level!r}: use one of {sorted(LEVEL_PLACEHOLDERS)}"]
    judge = config.get("llmAsAJudge")
    if not isinstance(judge, dict):
        return problems + ["config needs an llmAsAJudge object"]
    used = _placeholders(config)
    if not used:
        problems.append("instructions need at least one placeholder, e.g. {context} and {assistant_turn}")
    if bad := used - LEVEL_PLACEHOLDERS[level]:
        problems.append(f"placeholders {sorted(bad)} are not valid at {level} level")
    model_cfg = judge.get("modelConfig") or {}
    kind = next((k for k in MODEL_CONFIG_KINDS if k in model_cfg), None)
    judge_model = (model_cfg.get(kind) or {}).get("modelId", "") if kind else ""
    if not judge_model:
        problems.append(f"modelConfig needs one of {list(MODEL_CONFIG_KINDS)} with a modelId")
    if agent_model and judge_model and model_family(judge_model) == model_family(agent_model):
        problems.append(f"judge {judge_model} is the agent's own model family ({model_family(agent_model)}); "
                        "a judge scores its own family's answers high, so pick another family")
    return problems + _scale_problems(judge.get("ratingScale"))


def uses_ground_truth(config: dict[str, Any]) -> bool:
    """Ground-truth evaluators work in on-demand evaluations only, never in online configs."""
    return bool(_placeholders(config) & GROUND_TRUTH_PLACEHOLDERS)


def create(name: str, level: str, config: dict[str, Any], client: Any = None,
           agent_model: str | None = None) -> dict[str, Any]:
    if problems := validate(name, level, config, agent_model):
        raise ValueError("; ".join(problems))
    if client is None:
        import boto3

        client = boto3.client("bedrock-agentcore-control")
    # Returns evaluatorId (<name>-<10 chars>), ARN and status; batches and online configs take the ID.
    return client.create_evaluator(evaluatorName=name, level=level, evaluatorConfig=config)


def main(argv: list[str] | None = None, client: Any = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # a file name the console codepage can't encode must not crash
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Validate or create an AgentCore custom LLM-as-a-judge evaluator.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", nargs="+", metavar="CONFIG", help="validate configs, no AWS call")
    mode.add_argument("--create", metavar="CONFIG", help="validate one config, then CreateEvaluator")
    parser.add_argument("--name", help="evaluator name (--create); --check uses each file's stem")
    parser.add_argument("--level", default="TRACE", choices=sorted(LEVEL_PLACEHOLDERS))
    args = parser.parse_args(argv)
    agent = os.environ.get("AGENT_MODEL_ID")
    paths = args.check or [args.create]
    try:
        configs = {p: json.loads(Path(p).read_text(encoding="utf-8")) for p in paths}
    except (OSError, ValueError) as e:
        print(f"unreadable config: {e}", file=sys.stderr)
        return 2
    if args.check:
        failed = malformed = False
        for p, cfg in configs.items():
            if shape := shape_problems(cfg):
                malformed = True
                print(f"{p}: {'; '.join(shape)}")
                continue
            problems = validate(Path(p).stem, args.level, cfg, agent)
            failed |= bool(problems)
            note = " (ground truth: on-demand evaluations only)" if uses_ground_truth(cfg) else ""
            print(f"{p}: {'; '.join(problems) if problems else 'OK'}{note}")
        return 2 if malformed else 1 if failed else 0
    if not args.name:
        print("--create needs --name", file=sys.stderr)
        return 2
    cfg = configs[args.create]
    if shape := shape_problems(cfg):
        print("; ".join(shape), file=sys.stderr)
        return 2
    if problems := validate(args.name, args.level, cfg, agent):
        print("; ".join(problems), file=sys.stderr)
        return 1
    try:
        print(json.dumps(create(args.name, args.level, cfg, client, agent_model=agent), default=str, indent=2))
    except Exception as e:  # botocore errors, missing credentials
        print(f"CreateEvaluator failed: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

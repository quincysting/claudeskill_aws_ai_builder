"""Lint an Amazon States Language (Step Functions) definition for pitfalls seen in the
"Automate Fine-Tuning of an LLM" lab.

Checks (top level, and inside Map / Parallel branches):
- the main path (StartAt -> Next, Choice -> first unvisited non-Fail target) and, with --expect,
  whether the expected stages appear on it in that order;
- Next / Default / Catch targets that don't exist, and states that can't be reached;
- loops (e.g. Wait -> poll -> Choice -> Wait) with no top-level TimeoutSeconds;
- Choice states without Default;
- a state that reads a service-output field (`$.ModelArtifacts`, `$['Payload']`) from the root right
  after a state that nested its result under ResultPath `$.x` (the lab's DIY Create model bug);
- SageMaker createEndpoint / updateEndpoint with no later Wait state (i.e. no polling): they have no
  .sync pattern, so the execution can succeed while the endpoint is still Creating.

CLI:
  python asl_lint.py definition.json [--expect "Train,Create model,Create endpoint"]
  aws stepfunctions describe-state-machine --state-machine-arn ARN --query definition --output text | python asl_lint.py -
Exit codes: 0 no findings, 1 findings, 2 bad input (unreadable, not JSON, or not a state machine).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

_ROOT_KEY = re.compile(r"^\$(?:\.([A-Za-z_][\w-]*)|\['([^']+)'\])")
_NO_WAIT = ("sagemaker:createEndpoint", "sagemaker:updateEndpoint")  # also matches aws-sdk:sagemaker:...
_NESTED = ("ItemProcessor", "Iterator")


def validate(defn: Any, where: str = "definition") -> None:
    """Raise ValueError unless this looks like a state machine (StartAt, States of objects, string targets)."""
    if not isinstance(defn, Mapping):
        raise ValueError(f"{where} must be a JSON object")
    states = defn.get("States")
    if not isinstance(states, Mapping) or not states:
        raise ValueError(f"{where} has no States (piped the whole describe output? use --query definition)")
    if not isinstance(defn.get("StartAt"), str) or defn["StartAt"] not in states:
        raise ValueError(f"{where}: StartAt must name one of its States")
    for name, s in states.items():
        if not isinstance(s, Mapping):
            raise ValueError(f"{where}: state {name!r} is not an object")
        for k in ("Choices", "Catch", "Branches"):
            if not isinstance(s.get(k, []), list) or not all(isinstance(x, Mapping) for x in s.get(k, [])):
                raise ValueError(f"{where}: state {name!r} has a {k} that isn't a list of objects")
        for t in targets(s):
            if not isinstance(t, str):
                raise ValueError(f"{where}: state {name!r} has a non-string target {t!r}")
        for sub in _children(s):
            validate(sub, f"{where} -> {name}")


def targets(state: Mapping[str, Any], catch: bool = True) -> list[Any]:
    """States this one can transition to: Next, Choice branches, Default and (optionally) Catch."""
    out = [state["Next"]] if "Next" in state else []
    out += [c["Next"] for c in state.get("Choices", []) if isinstance(c, Mapping) and "Next" in c]
    out += [state["Default"]] if "Default" in state else []
    if catch:
        out += [c["Next"] for c in state.get("Catch", []) if isinstance(c, Mapping) and "Next" in c]
    return out


def _children(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Nested state machines of a Map (ItemProcessor / Iterator) or Parallel (Branches)."""
    subs = [state[k] for k in _NESTED if isinstance(state.get(k), Mapping)]
    return subs + [b for b in state.get("Branches", []) if isinstance(b, Mapping)]


def main_path(defn: Mapping[str, Any]) -> list[str]:
    """States in the order a normal run visits them. At a Choice, take the first of Default and the
    branch targets that isn't visited yet and isn't a Fail state, so a poll loop's way out is followed.
    ponytail: Default wins when it's unvisited, so a "nothing to do -> Succeed" Default hides the work branch."""
    states, path, name = defn["States"], [], defn["StartAt"]
    while name in states and name not in path:
        path.append(name)
        s = states[name]
        if s.get("Type") == "Choice":
            opts = [s.get("Default")] + [c.get("Next") for c in s.get("Choices", [])]
            name = next((o for o in opts if o in states and o not in path and states[o].get("Type") != "Fail"), None)
        else:
            name = s.get("Next")
    return path


def _reachable(states: Mapping[str, Any], start: str | None, catch: bool = True) -> set[str]:
    seen: set[str] = set()
    todo: list[Any] = [start]
    while todo:
        n = todo.pop()
        if n in seen or n not in states:
            continue
        seen.add(n)
        todo += targets(states[n], catch)
    return seen


def _cycle(states: Mapping[str, Any], start: str) -> list[str] | None:
    """One cycle as a list of state names, or None. Iterative DFS (generated machines can be long)."""
    color: dict[str, int] = {start: 1}
    stack = [(start, iter(targets(states[start])))]
    while stack:
        node, it = stack[-1]
        nxt = next(it, None)
        if nxt is None:
            color[node] = 2
            stack.pop()
        elif color.get(nxt) == 1:
            names = [n for n, _ in stack]
            return names[names.index(nxt):] + [nxt]
        elif nxt in states and not color.get(nxt):
            color[nxt] = 1
            stack.append((nxt, iter(targets(states[nxt]))))
    return None


def _waits(states: Mapping[str, Any], start: Any) -> bool:
    """Is there a Wait state on the Next path from `start` (Catch edges ignored), Map/Parallel included?"""
    for n in _reachable(states, start, catch=False):
        s = states[n]
        if s.get("Type") == "Wait" or any(_waits(c["States"], c["StartAt"]) for c in _children(s)):
            return True
    return False


def _paths(value: Any) -> Iterator[str]:
    """JSONPath strings in `"Key.$": "$..."` parameters, recursively."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if k.endswith(".$") and isinstance(v, str):
                yield v
            else:
                yield from _paths(v)
    elif isinstance(value, list):
        for v in value:
            yield from _paths(v)


def _root_key(path: str) -> str | None:
    m = _ROOT_KEY.match(path)  # "$$." (context object) and "States.*" intrinsics don't match
    return (m.group(1) or m.group(2)) if m else None


def _reads(state: Mapping[str, Any]) -> Iterator[str]:
    """JSONPaths a state reads from its input: Parameters, and Choice rule Variables."""
    yield from _paths(state.get("Parameters", {}))
    todo = list(state.get("Choices", []))
    while todo:
        rule = todo.pop()
        if isinstance(rule, Mapping):
            # Variable, plus comparisons against another path (StringEqualsPath, NumericLessThanPath, ...)
            yield from (v for k, v in rule.items() if (k == "Variable" or k.endswith("Path")) and isinstance(v, str))
            todo += [rule["Not"]] if isinstance(rule.get("Not"), Mapping) else []
            todo += [r for k in ("And", "Or") for r in rule.get(k, []) if isinstance(r, Mapping)]


def findings(defn: Mapping[str, Any], expect: Sequence[str] | None = None,
             prefix: str = "", bounded: bool = False, waits_after: bool = False) -> list[str]:
    """Findings for a definition (raises ValueError if it isn't one). `bounded`: a parent has TimeoutSeconds;
    `waits_after`: for a Map/Parallel branch, the parent waits after the Map/Parallel state."""
    if not prefix:
        validate(defn)
    states, out = defn["States"], []
    bounded = bounded or "TimeoutSeconds" in defn
    path = main_path(defn)
    if expect is not None:
        it = iter(path)
        if not all(x in it for x in expect):  # subsequence: polling states in between are fine
            out.append(f"order: expected {list(expect)} in that order, definition runs {path}")
    for name, s in states.items():
        for t in targets(s):
            if t not in states:
                out.append(f"{prefix}{name}: target {t!r} does not exist")
    for name in sorted(set(states) - _reachable(states, defn["StartAt"])):
        out.append(f"{prefix}unreachable: {name}")
    loop = _cycle(states, defn["StartAt"])
    if loop and not bounded:
        out.append(f"{prefix}loop without TimeoutSeconds: {' -> '.join(loop)} "
                   "(a Standard execution can run for up to a year)")
    for name, s in states.items():
        if s.get("Type") == "Choice" and "Default" not in s:
            out.append(f"{prefix}{name}: Choice without Default fails with States.NoChoiceMatched on an unexpected value")
        if s.get("Type") == "Task" and str(s.get("Resource", "")).endswith(_NO_WAIT):
            # ponytail: "waits" = any Wait state later on the Next path (Map/Parallel included, and the parent's
            # path for a branch). A Lambda-only poller with no Wait is still flagged, and any Wait counts even if
            # it doesn't poll the endpoint.
            if not (waits_after or _waits(states, s.get("Next"))):
                out.append(f"{prefix}{name}: {s['Resource'].rsplit(':', 1)[-1]} returns when the request is accepted; "
                           "nothing after it waits for the endpoint to be InService")
        nested = s.get("ResultPath")
        passthrough = s.get("OutputPath", "$") == "$"
        key = _root_key(nested) if isinstance(nested, str) and nested != "$" and passthrough else None
        for t in (targets(s, catch=False) if key else []):
            target = states.get(t, {})
            if target.get("InputPath", "$") != "$":
                continue
            for p in _reads(target):
                rk = _root_key(p)
                # ponytail: PascalCase root keys are taken to be service output (ModelArtifacts, Payload);
                # camelCase/snake_case keys are assumed to be execution input passed through. A PascalCase key
                # passed through from earlier (most often an earlier Lambda's Payload) is a known false positive.
                if rk and rk != key and rk[:1].isupper():
                    out.append(f"{prefix}{t}: reads {p} but {name} stored its result under {nested} "
                               f"(did you mean $.{key}.{rk}...?)")
        subs = _children(s)
        after = bool(subs) and (waits_after or _waits(states, s.get("Next")))  # only Map/Parallel need it
        for i, sub in enumerate(subs):
            out += findings(sub, None, f"{prefix}{name}[{i}]/", bounded, after)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Lint a Step Functions (ASL) definition")
    p.add_argument("definition", help="JSON file, or - for stdin")
    p.add_argument("--expect", help="comma-separated state names that should appear on the main path in this order")
    args = p.parse_args(argv)
    try:
        text = sys.stdin.read() if args.definition == "-" else Path(args.definition).read_text(encoding="utf-8-sig")
        defn = json.loads(text.lstrip("﻿"))
        validate(defn)
    except (OSError, ValueError) as e:  # json.JSONDecodeError and UnicodeDecodeError are ValueErrors
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    expect = [x.strip() for x in args.expect.split(",") if x.strip()] if args.expect else None
    found = findings(defn, expect)
    print("main path: " + " -> ".join(main_path(defn)))
    for f in found:
        print("- " + f)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

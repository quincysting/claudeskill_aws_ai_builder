"""Allow/deny regression table for Cedar policies behind an AgentCore MCP gateway. Stdlib only.

Usage: python policy_smoke_test.py --url https://<gateway-id>.gateway.bedrock-agentcore.REGION.amazonaws.com/mcp \
           --cases fixtures/policy_cases.example.json
       GATEWAY_TOKEN=<access token> python policy_smoke_test.py --url ... --cases ...   # CUSTOM_JWT inbound auth

Cases file: a JSON list of {"label", "tool" (<Target>___<tool>), "arguments" (object), "expect" ("allow"|"deny")}.

A policy denial comes back as JSON-RPC error -32002 ("Tool Execution Denied"). A tool's own error inside
`result` (for example "order not found") is still an ALLOW from the policy's point of view. Any other
error (bad tool name, HTTP 401/403 from the inbound authorizer, network failure) is reported as "error",
never as "deny", so an auth failure can't pass a deny case. The bearer token is read from the environment,
not argv, so it stays out of shell history. Gateways with AWS_IAM inbound auth need SigV4 signing, which
this script doesn't do.

Pick cases that don't change real data: deny cases are blocked before the target runs; for allow cases on
write tools use an id that doesn't exist where the tool reads before it writes, or an idempotent repeat.

Exit codes: 0 every case behaved, 1 a case did not, 2 bad arguments or an unreadable cases file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable

POLICY_DENIED = -32002
EXPECT = ("allow", "deny")


def post(url: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:  # the gateway sends JSON-RPC errors with HTTP 400; authorizers send 401/403
        body = e.read() or b""
        try:
            return json.loads(body) or {"error": {"code": e.code, "message": str(e)}}
        except ValueError:
            return {"error": {"code": e.code, "message": f"HTTP {e.code} with a non-JSON body"}}
    except (urllib.error.URLError, ValueError) as e:
        return {"error": {"code": None, "message": f"request failed: {e}"}}


def decision(reply: dict) -> str:
    """allow | deny | error (any error other than a policy denial)."""
    err = reply.get("error")
    if err is None:
        return "allow"
    return "deny" if isinstance(err, dict) and err.get("code") == POLICY_DENIED else "error"


def load_cases(path: str) -> list[dict]:
    """Read and check the cases file; raises ValueError with the first problem found."""
    with open(path, encoding="utf-8") as f:
        cases = json.load(f)
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases file must be a non-empty JSON list")
    for i, c in enumerate(cases, start=1):
        if not isinstance(c, dict) or not isinstance(c.get("label"), str) or not isinstance(c.get("tool"), str):
            raise ValueError(f"case {i}: needs string label and tool")
        if not isinstance(c.get("arguments"), dict):
            raise ValueError(f"case {i}: arguments must be an object")
        if c.get("expect") not in EXPECT:
            raise ValueError(f"case {i}: expect must be one of {EXPECT}")
    return cases


def run(url: str, cases: list[dict], send: Callable[[str, dict], dict]) -> bool:
    ok = True
    for i, c in enumerate(cases, start=1):
        reply = send(url, {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                           "params": {"name": c["tool"], "arguments": c["arguments"]}})
        got = decision(reply)
        ok &= got == c["expect"]
        err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        detail = err.get("message", "") or json.dumps(reply.get("result", {}))[:90]
        print(f"{'PASS' if got == c['expect'] else 'FAIL'}  {c['label']:<28} expect={c['expect']:<5} got={got:<5} {detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Allow/deny regression table for Cedar policies on a gateway.")
    parser.add_argument("--url", default=os.environ.get("GATEWAY_URL"), help="gateway MCP URL (or GATEWAY_URL)")
    parser.add_argument("--cases", required=True, help="JSON list of cases")
    args = parser.parse_args(argv)
    if not args.url or not args.url.startswith("https://"):
        print("--url (or GATEWAY_URL) must be an https gateway URL", file=sys.stderr)
        return 2
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as e:
        print(f"bad cases file: {e}", file=sys.stderr)
        return 2
    token = os.environ.get("GATEWAY_TOKEN")
    return 0 if run(args.url, cases, lambda u, p: post(u, p, token)) else 1


if __name__ == "__main__":
    sys.exit(main())

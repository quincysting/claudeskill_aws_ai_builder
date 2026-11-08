"""Offline tests for policy_smoke_test.py. Stdlib only, no network. Run: python -B -m unittest test_policy_smoke_test"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import policy_smoke_test as pst

CASES_FILE = Path(__file__).parent / "fixtures" / "policy_cases.example.json"


def fake_gateway(_url: str, payload: dict) -> dict:
    """Denies what the example policies deny: refunds of 250 or more, injected reasons, cancelling Shipped."""
    args = payload["params"]["arguments"]
    denied = (args.get("amount", 0) >= 250 or "Ignore all previous" in args.get("reason", "")
              or args.get("currentStatus") == "Shipped")
    if denied:
        return {"jsonrpc": "2.0", "id": payload["id"],
                "error": {"code": pst.POLICY_DENIED, "message": "Tool Execution Denied"}}
    return {"jsonrpc": "2.0", "id": payload["id"], "result": {"isError": False, "content": []}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPolicySmoke(unittest.TestCase):
    def setUp(self):
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)

    def test_decision(self):
        self.assertEqual(pst.decision({"result": {"isError": True}}), "allow")  # tool error = policy allowed
        self.assertEqual(pst.decision({"error": {"code": -32002}}), "deny")
        self.assertEqual(pst.decision({"error": {"code": -32601}}), "error")
        self.assertEqual(pst.decision({"error": "oops"}), "error")

    def test_example_cases_pass_and_a_wrong_expectation_fails(self):
        cases = pst.load_cases(str(CASES_FILE))
        self.assertTrue(pst.run("https://gw.example.com/mcp", cases, fake_gateway))
        flipped = [dict(cases[0], expect="deny")] + cases[1:]
        self.assertFalse(pst.run("https://gw.example.com/mcp", flipped, fake_gateway))

    def test_auth_failure_is_not_a_deny(self):
        """An HTML 403 from the inbound authorizer must not satisfy a deny case."""
        err = urllib.error.HTTPError("https://gw.example.com/mcp", 403, "Forbidden", {}, io.BytesIO(b"<html>no</html>"))
        with mock.patch.object(pst.urllib.request, "urlopen", side_effect=err):
            reply = pst.post("https://gw.example.com/mcp", {"id": 1})
        self.assertEqual(pst.decision(reply), "error")
        self.assertIn("403", reply["error"]["message"])

    def test_jsonrpc_error_with_http_400_is_parsed(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "denied"}}).encode()
        err = urllib.error.HTTPError("https://gw.example.com/mcp", 400, "Bad Request", {}, io.BytesIO(body))
        with mock.patch.object(pst.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(pst.decision(pst.post("https://gw.example.com/mcp", {"id": 1})), "deny")

    def test_bearer_token_sent_when_given(self):
        seen = {}

        def urlopen(req, timeout):
            seen.update(req.headers)
            return Resp(b'{"result": {}}')
        with mock.patch.object(pst.urllib.request, "urlopen", urlopen):
            pst.post("https://gw.example.com/mcp", {"id": 1}, token="abc")
        self.assertEqual(seen["Authorization"], "Bearer abc")

    def test_bad_inputs_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text(json.dumps([{"label": "x", "tool": "t", "arguments": {}, "expect": "maybe"}]), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, {"GATEWAY_URL": ""}):
                self.assertEqual(pst.main(["--url", "https://gw.example.com/mcp", "--cases", str(bad)]), 2)
                self.assertEqual(pst.main(["--url", "https://gw.example.com/mcp", "--cases", str(Path(d) / "none")]), 2)
                self.assertEqual(pst.main(["--url", "http://gw.example.com/mcp", "--cases", str(CASES_FILE)]), 2)
                self.assertEqual(pst.main(["--cases", str(CASES_FILE)]), 2)

    def test_main_exit_codes(self):
        with mock.patch.object(pst, "post", lambda u, p, t=None: fake_gateway(u, p)):
            self.assertEqual(pst.main(["--url", "https://gw.example.com/mcp", "--cases", str(CASES_FILE)]), 0)
        with mock.patch.object(pst, "post", lambda u, p, t=None: {"result": {}}):  # nothing denied
            self.assertEqual(pst.main(["--url", "https://gw.example.com/mcp", "--cases", str(CASES_FILE)]), 1)


if __name__ == "__main__":
    unittest.main()

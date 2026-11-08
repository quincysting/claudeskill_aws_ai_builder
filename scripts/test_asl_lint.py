"""Self-tests for asl_lint.py. Run: python -m unittest test_asl_lint -v (no AWS, no network)."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import asl_lint as m  # noqa: E402

SNAP = HERE / "fixtures"  # the lab 22 practice and DIY definitions
PRACTICE = json.loads((SNAP / "state_machine_definition.json").read_text(encoding="utf-8"))
DIY = json.loads((SNAP / "diy_state_machine_definition.json").read_text(encoding="utf-8"))
VALIDATOR_TEXT_ORDER = ["Train", "Create endpoint configuration", "Create model", "Create endpoint"]
CREATE_EP = "arn:aws:states:::sagemaker:createEndpoint"
DESCRIBE_EP = "arn:aws:states:::aws-sdk:sagemaker:describeEndpoint"


def task(next_=None, **kw):
    s = {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", **kw}
    if next_:
        s["Next"] = next_
    else:
        s["End"] = True
    return s


def endpoint_poll(after_create):
    """createEndpoint -> <after_create states> ; the dict passed in decides what follows."""
    return {"StartAt": "E", "TimeoutSeconds": 900, "States": {"E": task(after_create.pop("_next", None), Resource=CREATE_EP),
                                                              **after_create}}


def run(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(stdin or "")
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = m.main(argv)
    finally:
        sys.stdin = old
    return code, out.getvalue(), err.getvalue()


class LabDefinitions(unittest.TestCase):
    def test_practice_path_loop_and_endpoint(self):
        self.assertEqual(m.main_path(PRACTICE), [
            "Create training job", "Wait for training job", "Get training job status",
            "Response to training job status", "Create model", "Create endpoint configuration", "Create endpoint"])
        f = m.findings(PRACTICE)
        self.assertEqual(len(f), 2, f)
        self.assertTrue(f[0].startswith("loop without TimeoutSeconds: Wait for training job -> Get training job status"))
        self.assertIn("Create endpoint: createEndpoint returns", f[1])

    def test_practice_stage_order_without_listing_poll_states(self):
        stages = ["Create training job", "Create model", "Create endpoint configuration", "Create endpoint"]
        self.assertFalse(any(x.startswith("order") for x in m.findings(PRACTICE, stages)))

    def test_diy_resultpath_bug_and_order(self):
        self.assertEqual(m.main_path(DIY), ["Train", "Create model", "Create endpoint configuration", "Create endpoint"])
        f = m.findings(DIY, VALIDATOR_TEXT_ORDER)
        self.assertTrue(f[0].startswith("order: expected"))
        self.assertTrue(any("Create model: reads $['ModelArtifacts']['S3ModelArtifacts'] but Train stored its "
                            "result under $.train_step_result" in x for x in f), f)
        self.assertTrue(any(x.startswith("Create endpoint: createEndpoint") for x in f))
        self.assertFalse(any(x.startswith("order") for x in m.findings(DIY, m.main_path(DIY))))

    def test_diy_fixed_with_inputpath(self):
        fixed = json.loads(json.dumps(DIY))
        fixed["States"]["Create model"]["InputPath"] = "$.train_step_result"
        self.assertFalse(any("reads $['ModelArtifacts']" in x for x in m.findings(fixed)))


class MainPath(unittest.TestCase):
    def test_aws_job_poller_default_wait(self):
        d = {"StartAt": "Submit", "TimeoutSeconds": 600, "States": {
            "Submit": task("Wait"),
            "Wait": {"Type": "Wait", "Seconds": 30, "Next": "Get Status"},
            "Get Status": task("Job Complete?"),
            "Job Complete?": {"Type": "Choice", "Default": "Wait", "Choices": [
                {"Variable": "$.status", "StringEquals": "FAILED", "Next": "Job Failed"},
                {"Variable": "$.status", "StringEquals": "SUCCEEDED", "Next": "Get Final"}]},
            "Job Failed": {"Type": "Fail"},
            "Get Final": task()}}
        self.assertEqual(m.main_path(d), ["Submit", "Wait", "Get Status", "Job Complete?", "Get Final"])
        self.assertEqual(m.findings(d, ["Submit", "Get Final"]), [])

    def test_choice_without_default_last_branch_loops(self):
        d = {"StartAt": "W", "TimeoutSeconds": 60, "States": {
            "W": {"Type": "Wait", "Seconds": 5, "Next": "C"},
            "C": {"Type": "Choice", "Choices": [{"Variable": "$.s", "StringEquals": "done", "Next": "Next step"},
                                                {"Not": {"Variable": "$.s", "StringEquals": "done"}, "Next": "W"}]},
            "Next step": {"Type": "Succeed"}}}
        self.assertEqual(m.main_path(d), ["W", "C", "Next step"])


class Rules(unittest.TestCase):
    def test_clean_definition(self):
        d = {"StartAt": "A", "TimeoutSeconds": 60, "States": {"A": task("B"), "B": task()}}
        self.assertEqual(m.findings(d), [])

    def test_unreachable_and_missing_target(self):
        d = {"StartAt": "A", "States": {"A": task("Bb"), "B": task()}}
        self.assertEqual(m.findings(d), ["A: target 'Bb' does not exist", "unreachable: B"])

    def test_loop_ok_with_timeout(self):
        loop = {"StartAt": "W", "States": {
            "W": {"Type": "Wait", "Seconds": 5, "Next": "C"},
            "C": {"Type": "Choice", "Choices": [{"Variable": "$.s", "StringEquals": "x", "Next": "W"}], "Default": "Done"},
            "Done": {"Type": "Succeed"}}}
        self.assertTrue(m.findings(loop)[0].startswith("loop without TimeoutSeconds: W -> C -> W"))
        self.assertEqual(m.findings({**loop, "TimeoutSeconds": 3600}), [])

    def test_long_chain_no_recursion_error(self):
        states = {f"S{i}": {"Type": "Pass", "Next": f"S{i + 1}"} for i in range(3000)}
        states["S3000"] = {"Type": "Succeed"}
        self.assertEqual(m.findings({"StartAt": "S0", "States": states}), [])

    def test_choice_without_default(self):
        d = {"StartAt": "C", "States": {
            "C": {"Type": "Choice", "Choices": [{"Variable": "$.s", "StringEquals": "x", "Next": "D"}]},
            "D": {"Type": "Succeed"}}}
        self.assertEqual(m.findings(d), ["C: Choice without Default fails with States.NoChoiceMatched on an unexpected value"])

    def test_endpoint_rules(self):
        real_poll = {"_next": "Wait", "Wait": {"Type": "Wait", "Seconds": 60, "Next": "Describe"},
                     "Describe": task("Ready?", Resource=DESCRIBE_EP, ResultPath="$.ep"),
                     "Ready?": {"Type": "Choice", "Default": "Wait", "Choices": [
                         {"Variable": "$.ep.EndpointStatus", "StringEquals": "InService", "Next": "Done"}]},
                     "Done": {"Type": "Succeed"}}
        self.assertEqual(m.findings(endpoint_poll(dict(real_poll))), [])
        via_pass = {**real_poll, "_next": "P", "P": {"Type": "Pass", "Next": "Wait"}}
        self.assertEqual(m.findings(endpoint_poll(via_pass)), [])
        flagged = "createEndpoint returns when the request is accepted"
        single_describe = {"_next": "Describe", "Describe": task(None, Resource=DESCRIBE_EP)}  # returns "Creating"
        notify_only = {"_next": "Notify", "Notify": task(None, Resource="arn:aws:states:::sns:publish")}
        for case in (single_describe, notify_only):
            f = m.findings(endpoint_poll(case))
            self.assertEqual(len(f), 1, f)
            self.assertIn(flagged, f[0])
        catch_only = endpoint_poll({"Notify": task(None)})
        catch_only["States"]["E"]["Catch"] = [{"ErrorEquals": ["States.ALL"], "Next": "Notify"}]
        self.assertIn(flagged, m.findings(catch_only)[0])
        sdk_form = endpoint_poll({})
        sdk_form["States"]["E"]["Resource"] = "arn:aws:states:::aws-sdk:sagemaker:updateEndpoint"
        self.assertIn("updateEndpoint returns", m.findings(sdk_form)[0])
        cfg = {"StartAt": "C", "States": {"C": task(None, Resource="arn:aws:states:::sagemaker:createEndpointConfig")}}
        self.assertEqual(m.findings(cfg), [])

    def test_endpoint_waits_across_parallel(self):
        poll = {"Wait": {"Type": "Wait", "Seconds": 60, "Next": "Describe"}, "Describe": task(None, Resource=DESCRIBE_EP)}
        create_in_branches = {"StartAt": "Deploy", "TimeoutSeconds": 900, "States": {
            "Deploy": {"Type": "Parallel", "Next": "Wait", "Branches": [
                {"StartAt": "A", "States": {"A": task(None, Resource=CREATE_EP)}},
                {"StartAt": "B", "States": {"B": task(None, Resource=CREATE_EP)}}]},
            **poll}}
        self.assertEqual(m.findings(create_in_branches), [])
        poll_in_branch = {"StartAt": "E", "TimeoutSeconds": 900, "States": {
            "E": task("P", Resource=CREATE_EP),
            "P": {"Type": "Parallel", "End": True, "Branches": [{"StartAt": "Wait", "States": poll}]}}}
        self.assertEqual(m.findings(poll_in_branch), [])

    def test_resultpath_check_details(self):
        base = {"StartAt": "A", "States": {"A": task("B", ResultPath="$.a"), "B": task(Parameters={})}}
        for path, flagged in (("$.a.X", False), ("$['a']['X']", False), ("$.Other", True), ("$['Other']", True),
                              ("$.bucket", False), ("$$.Execution.Id", False), ("States.Format('{}', $.Other)", False)):
            base["States"]["B"]["Parameters"] = {"Nested": {"Value.$": path}}
            self.assertEqual(bool(m.findings(base)), flagged, path)
        base["States"]["B"]["Parameters"] = {"V.$": "$.Other"}
        for tweak in ({"ResultPath": "$"}, {"ResultPath": None}, {"ResultPath": "$.a", "OutputPath": "$.a"}):
            base["States"]["A"].update(tweak)
            self.assertEqual(m.findings(base), [], tweak)
            base["States"]["A"].pop("OutputPath", None)
        base["States"]["A"].update({"ResultPath": "$.a", "OutputPath": "$"})  # explicit defaults keep the rule on
        base["States"]["B"]["InputPath"] = "$"
        self.assertEqual(len(m.findings(base)), 1)

    def test_resultpath_choice_and_catch(self):
        d = {"StartAt": "A", "States": {
            "A": task("C", ResultPath="$.result", Catch=[{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "H"}]),
            "C": {"Type": "Choice", "Default": "Done", "Choices": [{"Variable": "$.Payload.status", "StringEquals": "x", "Next": "Done"}]},
            "H": task(None, Parameters={"Cause.$": "$.error.Cause"}),
            "Done": {"Type": "Succeed"}}}
        f = m.findings(d)
        self.assertEqual(len(f), 1, f)
        self.assertIn("C: reads $.Payload.status", f[0])
        d["States"]["C"]["Choices"] = [{"Variable": "$.result.x", "StringEqualsPath": "$.Payload.y", "Next": "Done"}]
        self.assertIn("C: reads $.Payload.y", m.findings(d)[0])

    def test_map_and_parallel_are_linted(self):
        inner = {"StartAt": "W", "States": {"W": {"Type": "Wait", "Seconds": 1, "Next": "C"},
                                            "C": {"Type": "Choice", "Choices": [{"Variable": "$.s", "StringEquals": "x", "Next": "W"}]},
                                            "Orphan": {"Type": "Pass", "End": True}}}
        d = {"StartAt": "M", "States": {
            "M": {"Type": "Map", "ItemProcessor": inner, "Next": "P"},
            "P": {"Type": "Parallel", "End": True, "Branches": [{"StartAt": "E", "States": {"E": task(None, Resource=CREATE_EP)}}]}}}
        f = m.findings(d)
        self.assertTrue(any(x.startswith("M[0]/loop without TimeoutSeconds") for x in f), f)
        self.assertTrue(any(x.startswith("M[0]/C: Choice without Default") for x in f), f)
        self.assertIn("M[0]/unreachable: Orphan", f)
        self.assertTrue(any(x.startswith("P[0]/E: createEndpoint") for x in f), f)
        self.assertFalse(any("loop" in x for x in m.findings({**d, "TimeoutSeconds": 60})))


class Validate(unittest.TestCase):
    def test_bad_structures(self):
        for bad in ({}, [], {"StartAt": "X", "States": {"A": task()}}, {"StartAt": [], "States": {"A": task()}},
                    {"StartAt": "A", "States": {"A": None}}, {"StartAt": "A", "States": {"A": {"Type": "Pass", "Next": ["B"]}}},
                    {"StartAt": "A", "States": {"A": {"Type": "Choice", "Choices": {}}}},
                    {"StartAt": "A", "States": {"A": {"Type": "Choice", "Choices": ["x"]}}},
                    {"StartAt": "A", "States": {"A": {"Type": "Parallel", "Branches": 3, "End": True}}},
                    {"StartAt": "M", "States": {"M": {"Type": "Map", "ItemProcessor": {"States": {}}, "End": True}}}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                m.validate(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                m.findings(bad)  # validates by itself when called as a library


class Cli(unittest.TestCase):
    def test_file_and_expect(self):
        code, out, _ = run([str(SNAP / "diy_state_machine_definition.json"), "--expect", ",".join(VALIDATOR_TEXT_ORDER)])
        self.assertEqual(code, 1)
        self.assertIn("main path: Train -> Create model -> Create endpoint configuration -> Create endpoint", out)
        self.assertIn("- order: expected", out)

    def test_stdin_clean(self):
        d = {"StartAt": "A", "States": {"A": task()}}
        self.assertEqual(run(["-"], json.dumps(d))[:2], (0, "main path: A\n"))

    def test_bom_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "def.json")
            f.write_text(json.dumps({"StartAt": "A", "States": {"A": task()}}), encoding="utf-8-sig")
            self.assertEqual(run([str(f)])[0], 0)

    def test_bad_input_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp, "bad.json")
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(run([str(bad)])[0], 2)
            self.assertEqual(run([str(Path(tmp, "missing.json"))])[0], 2)
        for text in ("[1, 2]", json.dumps({"StartAt": "X", "States": {}}),
                     json.dumps({"definition": "{}", "name": "whole describe output"}),
                     json.dumps({"StartAt": "A", "States": {"A": None}})):
            code, out, err = run(["-"], text)
            self.assertEqual(code, 2, text)
            self.assertNotIn("main path", out)
            self.assertTrue(err.startswith("error: ValueError"), err)


if __name__ == "__main__":
    unittest.main()

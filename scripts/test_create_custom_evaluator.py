"""Offline tests for create_custom_evaluator.py. Stdlib only, no AWS. Run: python -B -m unittest test_create_custom_evaluator"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import create_custom_evaluator as cce

CONCISE = {"llmAsAJudge": {
    "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": "us.amazon.nova-2-lite-v1:0"}},
    "instructions": "You are a performance evaluator. Judge only length and focus.\n{context}\n{assistant_turn}",
    "ratingScale": {"numerical": [
        {"value": 0, "label": "Not Concise", "definition": "Padding, lists or extras."},
        {"value": 0.5, "label": "Partially Concise", "definition": "Some padding."},
        {"value": 1, "label": "Concise", "definition": "One to three short sentences."}]}}}


def with_judge(**over) -> dict:
    cfg = copy.deepcopy(CONCISE)
    cfg["llmAsAJudge"].update(over)
    return cfg


class TestValidate(unittest.TestCase):
    def test_good_config(self):
        self.assertEqual(cce.validate("concise_reply", "TRACE", CONCISE), [])
        self.assertFalse(cce.uses_ground_truth(CONCISE))

    def test_placeholders(self):
        self.assertIn("at least one placeholder", cce.validate("x", "TRACE", with_judge(instructions="Rate it."))[0])
        self.assertTrue(cce.validate("concise", "SESSION", CONCISE))  # {assistant_turn} is trace-level only
        gt = with_judge(instructions="{assistant_turn} vs {expected_response}")
        self.assertEqual(cce.validate("gt_judge", "TRACE", gt), [])
        self.assertTrue(cce.uses_ground_truth(gt))

    def test_names(self):
        self.assertTrue(cce.validate("1bad-name", "TRACE", CONCISE))
        self.assertTrue(cce.validate("x" * 49, "TRACE", CONCISE))

    def test_rating_scales(self):
        categorical = with_judge(ratingScale={"categorical": [{"label": "Good", "definition": "Helpful."},
                                                              {"label": "Bad", "definition": "Not helpful."}]})
        self.assertEqual(cce.validate("cat_judge", "TRACE", categorical), [])
        dup = with_judge(ratingScale={"numerical": [{"value": 1, "label": "a", "definition": "d"},
                                                    {"value": 1, "label": "b", "definition": "d"}]})
        self.assertIn("distinct", cce.validate("dup", "TRACE", dup)[0])
        no_def = with_judge(ratingScale={"numerical": [{"value": 0, "label": "a"}, {"value": 1, "label": "b"}]})
        self.assertIn("definition", cce.validate("nodef", "TRACE", no_def)[0])
        both = with_judge(ratingScale={**CONCISE["llmAsAJudge"]["ratingScale"], **categorical["llmAsAJudge"]["ratingScale"]})
        self.assertIn("exactly one", cce.validate("both", "TRACE", both)[0])
        self.assertIn("exactly one", cce.validate("none", "TRACE", with_judge(ratingScale={}))[0])

    def test_model_config(self):
        self.assertIn("modelConfig", cce.validate("x", "TRACE", with_judge(modelConfig={}))[0])
        mantle = with_judge(modelConfig={"responsesEvaluatorModelConfig": {"modelId": "openai.gpt-oss-120b-1:0"}})
        self.assertEqual(cce.validate("mantle", "TRACE", mantle), [])

    def test_judge_family(self):
        self.assertEqual(cce.model_family("us.amazon.nova-2-lite-v1:0"), "amazon.nova")
        self.assertEqual(cce.model_family("amazon.nova-pro-v1:0"), "amazon.nova")
        self.assertIn("own model family", cce.validate("concise", "TRACE", CONCISE, "amazon.nova-pro-v1:0")[0])
        self.assertEqual(cce.validate("concise", "TRACE", CONCISE, "us.anthropic.claude-sonnet-4-5-20250929-v1:0"), [])


class FakeControl:
    def __init__(self):
        self.calls = []

    def create_evaluator(self, **kw):
        self.calls.append(kw)
        return {"evaluatorId": f"{kw['evaluatorName']}-AbCdE12345", "status": "ACTIVE"}


class TestCreateAndCli(unittest.TestCase):
    def test_create_validates_first(self):
        fake = FakeControl()
        self.assertTrue(cce.create("concise_reply", "TRACE", CONCISE, fake)["evaluatorId"].startswith("concise_reply-"))
        self.assertEqual(fake.calls[0]["evaluatorConfig"], CONCISE)
        with self.assertRaises(ValueError):
            cce.create("concise_reply", "SESSION", CONCISE, fake)
        self.assertEqual(len(fake.calls), 1)  # nothing sent for an invalid config

    def test_check_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            good, bad, broken = Path(d) / "concise_reply.json", Path(d) / "no_placeholder.json", Path(d) / "x.json"
            good.write_text(json.dumps(CONCISE), encoding="utf-8")
            bad.write_text(json.dumps(with_judge(instructions="Rate it.")), encoding="utf-8")
            broken.write_text("{not json", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cce.main(["--check", str(good)]), 0)
                self.assertEqual(cce.main(["--check", str(good), str(bad)]), 1)
                self.assertEqual(cce.main(["--check", str(good), "--level", "SESSION"]), 1)
                self.assertEqual(cce.main(["--check", str(broken)]), 2)
                self.assertEqual(cce.main(["--create", str(good)]), 2)  # --name missing

    def test_malformed_configs_return_2_and_keep_checking(self):
        shapes = {"toplevel_list": [], "model_str": with_judge(modelConfig="x"), "instr_int": with_judge(instructions=5),
                  "scale_entry": with_judge(ratingScale={"numerical": ["a", "b"]})}
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "concise_reply.json"
            good.write_text(json.dumps(CONCISE), encoding="utf-8")
            for name, cfg in shapes.items():
                with self.subTest(name):
                    p = Path(d) / f"{name}.json"
                    p.write_text(json.dumps(cfg), encoding="utf-8")
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(cce.main(["--check", str(p), str(good)]), 2)
                    self.assertIn("malformed", out.getvalue())
                    self.assertIn("concise_reply.json: OK", out.getvalue())  # the other file was still checked

    def test_arn_family(self):
        arn = "arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.amazon.nova-2-lite-v1:0"
        self.assertEqual(cce.model_family(arn), "amazon.nova")

    def test_create_with_injected_client_rc_0_1_2(self):
        with tempfile.TemporaryDirectory() as d:
            good, bad, odd = Path(d) / "g.json", Path(d) / "b.json", Path(d) / "o.json"
            good.write_text(json.dumps(CONCISE), encoding="utf-8")
            bad.write_text(json.dumps(with_judge(instructions="Rate it.")), encoding="utf-8")
            odd.write_text("[]", encoding="utf-8")
            fake = FakeControl()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cce.main(["--create", str(good), "--name", "concise_reply"], client=fake), 0)
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(cce.main(["--create", str(bad), "--name", "x"], client=fake), 1)
                self.assertEqual(cce.main(["--create", str(odd), "--name", "x"], client=fake), 2)
            self.assertEqual(len(fake.calls), 1)


if __name__ == "__main__":
    unittest.main()

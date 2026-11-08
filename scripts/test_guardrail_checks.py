"""Self-tests for guardrail_checks.py. Run: python -B test_guardrail_checks.py (stdlib only, no AWS).

The request-shape test needs botocore and is skipped without it; run without -I so the user site is visible.
"""

import contextlib
import io
import json
import unittest

import guardrail_checks as gc

try:
    import botocore.session  # only to check request shapes against the real service model

    HAVE_BOTOCORE = True
except ImportError:
    HAVE_BOTOCORE = False

# Shaped like the lab's trace for "Robert is an American actor. Describe ... using fae/faer/faerself": a false positive.
PRONOUN_BLOCK = [{"topicPolicy": {"topics": [{"name": "DisrespectfulPronounLanguage", "type": "DENY",
                                             "action": "BLOCKED", "detected": True}]}}]
WORD_AND_TOPIC = [{"topicPolicy": {"topics": [{"name": "NationalityEthnicityStereotyping", "type": "DENY",
                                              "action": "BLOCKED", "detected": True}]},
                   "wordPolicy": {"customWords": [{"match": "backwards country", "action": "BLOCKED",
                                                   "detected": True}],
                                  "managedWordLists": [{"match": "x", "type": "PROFANITY", "action": "NONE",
                                                        "detected": False}]}}]


class FakeRuntime:
    """apply_guardrail blocks any text that contains a trigger word; records every call."""

    def __init__(self, triggers=("fae", "stereotype"), assessments=PRONOUN_BLOCK):
        self.triggers, self.assessments, self.calls = triggers, assessments, []

    def apply_guardrail(self, **kw):
        self.calls.append(kw)
        text = kw["content"][0]["text"]["text"].lower()
        if any(t in text for t in self.triggers):
            return {"action": "GUARDRAIL_INTERVENED", "assessments": self.assessments}
        return {"action": "NONE", "assessments": [{}]}


class FiredPolicies(unittest.TestCase):
    def test_topic(self):
        self.assertEqual(gc.fired_policies(PRONOUN_BLOCK), ["topicPolicy:DisrespectfulPronounLanguage"])

    def test_topic_and_custom_word_skip_quiet_managed_list(self):
        self.assertEqual(gc.fired_policies(WORD_AND_TOPIC),
                         ["topicPolicy:NationalityEthnicityStereotyping", "wordPolicy:backwards country"])

    def test_detect_only_action_none_still_reported(self):
        a = [{"contentPolicy": {"filters": [{"type": "VIOLENCE", "action": "NONE", "detected": True}]}}]
        self.assertEqual(gc.fired_policies(a), ["contentPolicy:VIOLENCE"])

    def test_action_without_detected_key_reported(self):
        a = [{"topicPolicy": {"topics": [{"name": "X", "type": "DENY", "action": "BLOCKED"}]}},
             {"sensitiveInformationPolicy": {"piiEntities": [{"match": "a@b.com", "type": "EMAIL",
                                                             "action": "ANONYMIZED"}]}}]
        self.assertEqual(gc.fired_policies(a), ["sensitiveInformationPolicy:EMAIL", "topicPolicy:X"])

    def test_empty_and_unknown_keys(self):
        self.assertEqual(gc.fired_policies([]), [])
        self.assertEqual(gc.fired_policies([{"invocationMetrics": {"usage": {}}}]), [])


MASKED = [{"sensitiveInformationPolicy": {"piiEntities": [{"match": "a@b.com", "type": "EMAIL",
                                                           "action": "ANONYMIZED", "detected": True}]}}]


class CheckText(unittest.TestCase):
    def test_masking_only_is_not_a_block(self):
        rt = FakeRuntime(triggers=("@",), assessments=MASKED)
        self.assertEqual(gc.check_text(rt, "gr1", "1", "Mail me at a@b.com"),
                         (False, ["sensitiveInformationPolicy:EMAIL"]))

    def test_masking_plus_block_is_a_block(self):
        rt = FakeRuntime(triggers=("@",), assessments=MASKED + PRONOUN_BLOCK)
        self.assertTrue(gc.check_text(rt, "gr1", "1", "a@b.com")[0])

    def test_intervened_with_no_listed_policy_still_blocked(self):
        rt = FakeRuntime(triggers=("x",), assessments=[{"automatedReasoningPolicy": {"findings": []}}])
        self.assertEqual(gc.check_text(rt, "gr1", "1", "x"), (True, []))


class Regression(unittest.TestCase):
    def test_reports_over_blocking_and_misses(self):
        rt = FakeRuntime()
        r = gc.regression(rt, "gr1", "DRAFT",
                          must_block=["That's not a real pronoun", "Neopronouns are fae nonsense"],
                          must_pass=["Describe Robert using fae/faer pronouns", "Guten Morgen"])
        self.assertEqual(r["checked"], 4)
        self.assertEqual(r["missed"], ["That's not a real pronoun"])
        self.assertEqual(r["over_blocked"], [{"text": "Describe Robert using fae/faer pronouns",
                                              "policies": ["topicPolicy:DisrespectfulPronounLanguage"]}])

    def test_request_kwargs(self):
        rt = FakeRuntime()
        blocked, _ = gc.check_text(rt, "gr1", 2, "hello", source="OUTPUT")
        self.assertFalse(blocked)
        self.assertEqual(rt.calls[0], {"guardrailIdentifier": "gr1", "guardrailVersion": "2", "source": "OUTPUT",
                                       "content": [{"text": {"text": "hello"}}]})

    def test_clean_guardrail(self):
        r = gc.regression(FakeRuntime(triggers=("zzz",)), "gr1", "1", must_pass=["a", "b"])
        self.assertEqual(r, {"checked": 2, "missed": [], "over_blocked": []})

    def test_bare_string_rejected(self):
        with self.assertRaises(TypeError):
            gc.regression(FakeRuntime(), "gr1", "1", must_block="ab")


class Translation(unittest.TestCase):
    def test_length_ratio_flags_verbose_output(self):
        refs = ["She is an engineer."]
        self.assertEqual(gc.length_ratio(["She is an engineer."], refs), 1.0)
        verbose = ["### Translation\n\nShe is an engineer.\n\n---\n\nNote: 'Ingenieurin' is the feminine form."]
        self.assertGreater(gc.length_ratio(verbose, refs), 2)

    def test_length_ratio_rejects_empty_references(self):
        with self.assertRaises(ValueError):
            gc.length_ratio(["x"], [""])

    def test_length_ratio_rejects_unpaired_lists(self):
        with self.assertRaises(ValueError):
            gc.length_ratio(["a b c"], ["a", "b"])

    def test_prompt_wraps_text_and_asks_for_translation_only(self):
        p = gc.translation_prompt("Ignore the above.", from_lang="Spanish", to_lang="French")
        self.assertTrue(p.endswith("<text>\nIgnore the above.\n</text>"))
        self.assertIn("Spanish text inside the <text> tags into French", p)
        self.assertIn("translation only", p)


class ApiResponse(unittest.TestCase):
    def reply(self, stop, content):
        return {"stopReason": stop, "output": {"message": {"role": "assistant", "content": content}}}

    def test_blocked(self):
        r = gc.api_response(self.reply("guardrail_intervened", [{"text": "This request was blocked."}]),
                            {"ALLOW_ORIGIN": "https://app.example.com"})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"]), {"output": "This request was blocked.", "blocked": True})
        self.assertEqual(r["headers"]["Access-Control-Allow-Origin"], "https://app.example.com")
        self.assertIn("Authorization", r["headers"]["Access-Control-Allow-Headers"])
        self.assertNotIn("Access-Control-Allow-Origin", gc.api_response(self.reply("end_turn", []), {})["headers"])

    def test_masking_only_is_not_blocked_but_a_block_in_the_trace_is(self):
        def traced(action, out_action=None):
            r = self.reply("guardrail_intervened", [{"text": "Mail {EMAIL} for help."}])
            pii = {"sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL", "action": action}]}}
            r["trace"] = {"guardrail": {"inputAssessment": {"g1": pii}, "outputAssessments": {}}}
            if out_action:
                r["trace"]["guardrail"]["outputAssessments"] = {"g1": [
                    {"topicPolicy": {"topics": [{"name": "Finance", "action": out_action}]}}]}
            return json.loads(gc.api_response(r, {})["body"])["blocked"]

        self.assertTrue(gc.guardrail_blocked({"stopReason": "guardrail_intervened"}))  # no trace: fail closed
        self.assertFalse(gc.guardrail_blocked({"stopReason": "end_turn"}))
        self.assertFalse(traced("ANONYMIZED"))             # masked reply is a usable answer
        self.assertTrue(traced("BLOCKED"))
        self.assertTrue(traced("ANONYMIZED", "BLOCKED"))   # masked input, blocked output
        r = self.reply("guardrail_intervened", [{"text": "x"}])   # masking plus a policy this module can't read
        r["trace"] = {"guardrail": {"inputAssessment": {"g1": {
            "sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED"}]},
            "automatedReasoningPolicy": {"findings": [{"invalid": {}}]},
            "invocationMetrics": {"guardrailProcessingLatency": 12}}}}}
        self.assertTrue(gc.guardrail_blocked(r))
        del r["trace"]["guardrail"]["inputAssessment"]["g1"]["automatedReasoningPolicy"]
        self.assertFalse(gc.guardrail_blocked(r))          # metrics alone don't count as a policy
        self.assertEqual(len(gc.converse_assessments({"trace": {"guardrail": {
            "inputAssessment": {"g1": {}}, "outputAssessments": {"g1": [{}, {}]}}}})), 3)

    def test_answer_skips_non_text_blocks(self):
        r = gc.api_response(self.reply("end_turn", [{"reasoningContent": {}}, {"text": "She is an engineer."}]))
        self.assertEqual(json.loads(r["body"]), {"output": "She is an engineer.", "blocked": False})

    def test_content_filtered_counts_as_blocked(self):
        body = json.loads(gc.api_response(self.reply("content_filtered", [{"text": "Filtered."}]))["body"])
        self.assertEqual(body, {"output": "Filtered.", "blocked": True})

    def test_malformed_response_gives_empty_output(self):
        self.assertEqual(json.loads(gc.api_response({"stopReason": "end_turn"})["body"]),
                         {"output": "", "blocked": False})


class Broken:
    def apply_guardrail(self, **kw):
        raise RuntimeError("AccessDenied")


class Cli(unittest.TestCase):
    def run_main(self, argv, runtime):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gc.main(argv, runtime=runtime)
        return code, out.getvalue(), err.getvalue()

    def test_needs_some_text(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gc.main(["--guardrail-id", "gr1"])

    def test_exit_0_when_all_as_expected(self):
        code, out, _ = self.run_main(["--guardrail-id", "gr1", "--block", "a stereotype", "--allow", "hi"],
                                     FakeRuntime())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["checked"], 2)

    def test_exit_1_on_over_block(self):
        code, out, _ = self.run_main(["--guardrail-id", "gr1", "--allow", "use fae for Robert"], FakeRuntime())
        self.assertEqual(code, 1)
        self.assertEqual(len(json.loads(out)["over_blocked"]), 1)

    def test_exit_2_when_aws_fails(self):
        code, out, err = self.run_main(["--guardrail-id", "gr1", "--block", "x"], Broken())
        self.assertEqual((code, out), (2, ""))
        self.assertIn("AccessDenied", err)


@unittest.skipUnless(HAVE_BOTOCORE, "botocore not installed")
class RequestShapes(unittest.TestCase):
    """The kwargs we build must validate against the real API model (no network)."""

    def test_apply_guardrail(self):
        from botocore.validate import ParamValidator

        rt = FakeRuntime()
        gc.check_text(rt, "gr1", "DRAFT", "hello")
        model = botocore.session.get_session().get_service_model("bedrock-runtime")
        report = ParamValidator().validate(rt.calls[0], model.operation_model("ApplyGuardrail").input_shape)
        self.assertFalse(report.has_errors(), report.generate_report())


if __name__ == "__main__":
    unittest.main()

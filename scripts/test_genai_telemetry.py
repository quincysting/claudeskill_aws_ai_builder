"""Offline tests for genai_telemetry.py. Stdlib only. Run: python -B -m unittest test_genai_telemetry"""
from __future__ import annotations

import json
import unittest

import genai_telemetry as gt

PRICES = {"input": 0.0008, "output": 0.0032, "cache_read": 0.00008, "cache_write": 0.0010}


class TestUsage(unittest.TestCase):
    def test_input_includes_cache_tokens(self):
        attrs = gt.usage_attributes({"inputTokens": 50, "outputTokens": 20,
                                     "cacheReadInputTokens": 1200, "cacheWriteInputTokens": 30})
        self.assertEqual(attrs, {gt.INPUT: 1280, gt.OUTPUT: 20, gt.CACHE_READ: 1200, gt.CACHE_WRITE: 30})

    def test_no_cache_point(self):
        self.assertEqual(gt.usage_attributes({"inputTokens": 50, "outputTokens": 20}),
                         {gt.INPUT: 50, gt.OUTPUT: 20, gt.CACHE_READ: 0, gt.CACHE_WRITE: 0})

    def test_required_counts_and_none_cache_counts(self):
        for usage in ({"outputTokens": 1}, {"inputTokens": 1}, {"inputTokens": None, "outputTokens": 1}):
            with self.assertRaises(ValueError):
                gt.usage_attributes(usage)
        with self.assertRaisesRegex(ValueError, "inputTokens"):
            gt.usage_attributes({"outputTokens": 1})
        attrs = gt.usage_attributes({"inputTokens": 5, "outputTokens": 1,
                                     "cacheReadInputTokens": None, "cacheWriteInputTokens": None})
        self.assertEqual((attrs[gt.CACHE_READ], attrs[gt.CACHE_WRITE]), (0, 0))

    def test_attribute_names(self):
        self.assertEqual(gt.CACHE_READ, "gen_ai.usage.cache_read.input_tokens")
        self.assertEqual(gt.CACHE_WRITE, "gen_ai.usage.cache_creation.input_tokens")


class TestCost(unittest.TestCase):
    def test_cache_split(self):
        write_heavy = gt.usage_attributes({"inputTokens": 50, "outputTokens": 20, "cacheWriteInputTokens": 1200})
        read_heavy = gt.usage_attributes({"inputTokens": 50, "outputTokens": 20, "cacheReadInputTokens": 1200})
        self.assertAlmostEqual(gt.cost_usd(write_heavy, PRICES), (50 * 0.0008 + 20 * 0.0032 + 1200 * 0.0010) / 1000)
        self.assertAlmostEqual(gt.cost_usd(read_heavy, PRICES), (50 * 0.0008 + 20 * 0.0032 + 1200 * 0.00008) / 1000)
        self.assertGreater(gt.cost_usd(write_heavy, PRICES), 4 * gt.cost_usd(read_heavy, PRICES))

    def test_raw_usage_or_missing_price_refused(self):
        with self.assertRaises(ValueError):
            gt.cost_usd({gt.INPUT: 50, gt.CACHE_READ: 1200}, PRICES)  # raw Bedrock counts, not attrs
        with self.assertRaises(ValueError):
            gt.cost_usd(gt.usage_attributes({"inputTokens": 1}), {"input": 1.0})


class TestBestEffort(unittest.TestCase):
    def test_failure_counted_not_raised(self):
        def down(**_kw):
            raise RuntimeError("sink unavailable")
        before = gt.DROPPED["ledger"]
        with self.assertLogs("genai_telemetry", level="WARNING") as logs:
            self.assertFalse(gt.best_effort("ledger", down, Item={}))
        self.assertEqual(gt.DROPPED["ledger"], before + 1)
        self.assertEqual(json.loads(logs.records[0].getMessage()),
                         {"telemetry_dropped": "ledger", "error": "RuntimeError"})

    def test_success(self):
        rows = []
        self.assertTrue(gt.best_effort("ledger", rows.append, {"a": 1}))
        self.assertEqual(rows, [{"a": 1}])


if __name__ == "__main__":
    unittest.main()

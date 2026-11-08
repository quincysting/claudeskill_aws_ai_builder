"""Offline tests for firehose_transform.py (no AWS: the handler is called with Firehose-shaped events).

Run: python -B -m unittest test_firehose_transform
"""
from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import firehose_transform as ft  # noqa: E402


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def event(*records) -> dict:
    return {"records": [{"recordId": str(i), "data": data} for i, data in enumerate(records)]}


class HandlerTest(unittest.TestCase):
    def tearDown(self):
        ft.EMIT_PARTITION_KEYS = False

    def test_ok_record_round_trips_as_base64_json_line(self):
        out = ft.handler(event(b64({"ts": "2026-05-04T20:01:00Z"})), None)["records"]
        self.assertEqual([(r["recordId"], r["result"]) for r in out], [("0", "Ok")])
        raw = base64.b64decode(out[0]["data"]).decode()
        self.assertTrue(raw.endswith("\n"), "one JSON object per line in S3")
        body = json.loads(raw)
        self.assertEqual(body["ts"], "2026-05-04T20:01:00Z")
        self.assertIn("processed_at", body)
        self.assertNotIn("metadata", out[0])

    def test_bad_records_fail_alone_and_keep_their_data(self):
        good, not_b64, a_list = b64({"a": 1}), "!!notb64", b64([1, 2])
        out = ft.handler(event(good, not_b64, a_list, base64.b64encode(b"\xff\xfe").decode()), None)["records"]
        self.assertEqual([r["result"] for r in out], ["Ok", "ProcessingFailed", "ProcessingFailed", "ProcessingFailed"])
        self.assertEqual(out[1]["data"], not_b64)
        self.assertEqual([r["recordId"] for r in out], ["0", "1", "2", "3"], "one result per input, same ids")

    def test_partition_keys_from_the_timestamp(self):
        ft.EMIT_PARTITION_KEYS = True
        out = ft.handler(event(b64({"ts": "2026-05-04T20:01:00Z"})), None)["records"][0]
        self.assertEqual(out["metadata"]["partitionKeys"], {"year": "2026", "month": "05", "day": "04"})

    def test_no_bogus_partition_when_the_date_is_missing_or_garbage(self):
        ft.EMIT_PARTITION_KEYS = True
        out = ft.handler(event(b64({"ts": "garbage-value"}), b64({"other": 1}), b64({"ts": 20260504})), None)
        self.assertEqual([r["result"] for r in out["records"]], ["ProcessingFailed"] * 3)

    def test_transform_error_fails_only_that_record(self):
        orig = ft.transform
        ft.transform = lambda p: (_ for _ in ()).throw(ValueError("boom")) if p.get("bad") else {**p, "ok": 1}
        try:
            with self.assertLogs("firehose_transform", level="WARNING") as logs:
                out = ft.handler(event(b64({"a": 1}), b64({"bad": "secret-payload"}), b64({"b": 2})), None)["records"]
        finally:
            ft.transform = orig
        self.assertEqual([r["result"] for r in out], ["Ok", "ProcessingFailed", "Ok"])
        self.assertNotIn("secret-payload", "".join(logs.output))
        self.assertIn("ValueError", "".join(logs.output))

    def test_unencodable_transform_result_fails_only_that_record(self):
        orig = ft.transform
        ft.transform = lambda p: {"x": object()} if p.get("bad") else p
        try:
            with self.assertLogs("firehose_transform", level="WARNING"):
                out = ft.handler(event(b64({"a": 1}), b64({"bad": 1})), None)["records"]
        finally:
            ft.transform = orig
        self.assertEqual([r["result"] for r in out], ["Ok", "ProcessingFailed"])

    def test_partition_uses_utc_and_rejects_invalid_or_naive(self):
        ft.EMIT_PARTITION_KEYS = True
        good = ft.handler(event(b64({"ts": "2026-05-04T23:30:00-05:00"})), None)["records"][0]
        self.assertEqual(good["metadata"]["partitionKeys"], {"year": "2026", "month": "05", "day": "05"})
        bad = ["2026-13-45T00:00:00Z", "٢٠٢٦-05-04T00:00:00Z", "2026-05-04T20:01:00",
               "2026-05-04"]
        out = ft.handler(event(*[b64({"ts": t}) for t in bad]), None)["records"]
        self.assertEqual([r["result"] for r in out], ["ProcessingFailed"] * len(bad))

    def test_parse_flag(self):
        for v in ("1", "true", "TRUE", "Yes"):
            self.assertTrue(ft._parse_flag(v), v)
        for v in ("0", "false", "No", ""):
            self.assertFalse(ft._parse_flag(v), v)
        with self.assertRaises(ValueError):
            ft._parse_flag("ture")


if __name__ == "__main__":
    unittest.main()

"""Amazon Data Firehose transformation Lambda: decode, enrich, re-encode, one result per input record.

Contract (anything else fails the batch with Lambda.FunctionError / ProcessingFailed):
    return {"records": [{"recordId": <same as input>, "result": "Ok"|"Dropped"|"ProcessingFailed",
                         "data": <base64 of the new payload>}, ...]}
This is the shape for Kinesis Data Streams and Direct PUT sources (Firehose's data-transformation-status-model
page). An MSK source sends and expects kafkaRecordValue instead of data, so don't put this handler on one as is.

- A record that isn't base64 JSON, or isn't a JSON object, comes back ProcessingFailed with its original
  data, so it lands under the processing-failed prefix instead of failing the whole batch.
- A trailing newline gives one JSON object per line in S3, which Athena and Glue read directly.
- EMIT_PARTITION_KEYS=true also returns metadata.partitionKeys (year, month, day from the ISO-8601 field
  TS_FIELD) for dynamic partitioning by Lambda; use !{partitionKeyFromLambda:year} in the S3 prefix.
  The field is parsed as ISO-8601, converted to UTC and partitioned on the UTC date. A record with no valid
  date, or a timestamp without a timezone (naive), comes back ProcessingFailed rather than landing in a bogus
  or ambiguous partition.
- EMIT_PARTITION_KEYS accepts 1/true/yes (on) and 0/false/no/empty (off), any case; anything else raises
  ValueError at import.
- If transform() or the encoding raises, only that record comes back ProcessingFailed (its ID and the
  exception class are logged, never the payload), so one bad record does not make Firehose retry the batch.
- Request and response are each capped at 6 MB (synchronous Lambda invoke). Enrichment grows the response,
  so keep the Lambda buffer hint well under that.

Standard library only. Tests: test_firehose_transform.py.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)


def _parse_flag(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no", ""):
        return False
    raise ValueError(f"EMIT_PARTITION_KEYS={value!r}: use 1/true/yes or 0/false/no")


TS_FIELD = os.environ.get("TS_FIELD", "ts")
EMIT_PARTITION_KEYS = _parse_flag(os.environ.get("EMIT_PARTITION_KEYS", "false"))


def _partition_keys(ts: object) -> dict | None:
    """UTC year/month/day of an ISO-8601 string with a timezone, else None."""
    if not isinstance(ts, str):
        return None
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    when = when.astimezone(timezone.utc)
    return {"year": f"{when.year:04d}", "month": f"{when.month:02d}", "day": f"{when.day:02d}"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def transform(payload: dict) -> dict:
    """Business enrichment. Returns a new dict; replace with your own logic."""
    return {**payload, "processed_at": _now_iso()}


def _encode(obj: dict) -> str:
    return base64.b64encode((json.dumps(obj) + "\n").encode("utf-8")).decode("utf-8")


def _failed(rec: dict) -> dict:
    return {"recordId": rec["recordId"], "result": "ProcessingFailed", "data": rec["data"]}


def handler(event: dict, context: object) -> dict:
    out = []
    for rec in event["records"]:
        try:
            payload = json.loads(base64.b64decode(rec["data"], validate=True))
        except (ValueError, TypeError):  # binascii.Error, JSONDecodeError, UnicodeDecodeError
            payload = None
        if not isinstance(payload, dict):
            out.append(_failed(rec))
            continue
        try:
            result = {"recordId": rec["recordId"], "result": "Ok", "data": _encode(transform(payload))}
            if EMIT_PARTITION_KEYS:
                keys = _partition_keys(payload.get(TS_FIELD))
                if keys is None:
                    out.append(_failed(rec))
                    continue
                result["metadata"] = {"partitionKeys": keys}
        except Exception as exc:  # one bad record must not fail the whole batch
            _logger.warning("record %s failed: %s", rec["recordId"], type(exc).__name__)
            out.append(_failed(rec))
            continue
        out.append(result)
    return {"records": out}

"""Offline self-tests for dynamodb_state.py. Run: python -B -m unittest test_dynamodb_state (no AWS, no network).

A dict-backed fake table honours ConditionExpression atomically and stores items the way DynamoDB does
(ints come back as Decimal, floats are refused). The wire round trip reads the checkpoint back in a separate
Python process from DynamoDB JSON; it needs boto3's type serializer and is skipped without boto3.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import threading
import unittest
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dynamodb_state as st  # noqa: E402

try:
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

    HAVE_BOTO3 = True
except ImportError:
    HAVE_BOTO3 = False


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: dynamodb_state reads only .response["Error"]["Code"]."""

    def __init__(self, code: str, item: dict | None = None) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}
        if item is not None:
            self.response["Item"] = item


def _ddb(value):
    """What DynamoDB stores and hands back: int becomes Decimal, float is refused."""
    if value is None or isinstance(value, (bool, str, Decimal)):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, dict):
        return {k: _ddb(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_ddb(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return {_ddb(v) for v in value}
    raise TypeError(f"Unsupported type {type(value)}")


def _holds(expr: str, item: dict | None, names: dict, values: dict) -> bool:
    """attribute_exists(a), attribute_not_exists(a) and a = :v joined by AND, with #name and :value checks."""
    tokens = set(re.findall(r"[#:]\w+", expr))
    if tokens - set(names) - set(values) or set(names) - tokens or set(values) - tokens:
        raise FakeClientError("ValidationException")
    for clause in re.split(r"\s+AND\s+", expr.strip()):
        if m := re.fullmatch(r"(attribute_exists|attribute_not_exists)\((\S+)\)", clause):
            present = item is not None and names.get(m[2], m[2]) in item
            if present != (m[1] == "attribute_exists"):
                return False
        elif m := re.fullmatch(r"(\S+)\s*=\s*(:\w+)", clause):
            if item is None or item.get(names.get(m[1], m[1])) != values[m[2]]:
                return False
        else:
            raise NotImplementedError(clause)
    return True


class FakeTable:
    """DynamoDB Table stand-in. A conditional put is atomic (one lock), as it is in DynamoDB."""

    def __init__(self, key: str) -> None:
        self.key, self.rows, self.lock, self.consistent = key, {}, threading.Lock(), []

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None,  # noqa: N803
                 ExpressionAttributeValues=None, ReturnValuesOnConditionCheckFailure=None):  # noqa: N803
        with self.lock:
            current = self.rows.get(Item[self.key])
            if ConditionExpression and not _holds(ConditionExpression, current, ExpressionAttributeNames or {},
                                                  _ddb(ExpressionAttributeValues or {})):
                old = copy.deepcopy(current) if ReturnValuesOnConditionCheckFailure == "ALL_OLD" else None
                raise FakeClientError("ConditionalCheckFailedException", old)
            self.rows[Item[self.key]] = _ddb(Item)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        self.consistent.append(ConsistentRead)
        row = self.rows.get(Key[self.key])
        return {"Item": copy.deepcopy(row)} if row is not None else {}

    def scan(self, ConsistentRead=False, **_):  # noqa: N803
        self.consistent.append(ConsistentRead)
        return {"Items": [copy.deepcopy(r) for r in self.rows.values()]}


# ---------- checkpoint types (module level, as an app's types module would be) ----------

class State(str, Enum):
    INTAKE = "INTAKE"
    WORKING = "WORKING"
    PEND = "PEND"


class Result(str, Enum):
    MET = "met"
    NOT_MET = "not_met"


@dataclass
class Step:
    result: Result
    decided_at: datetime
    notes: dict | None = None


@dataclass
class Checkpoint:
    request_id: str
    state: State
    amount: Decimal
    codes: set[str]
    received: date
    step: Step | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    history: list[State] = field(default_factory=list)
    window: tuple[date, date] | None = None


@dataclass
class WithDerived:
    request_id: str
    derived: int = field(init=False, default=0)


@dataclass
class Strict:
    request_id: str
    count: int
    tags: set[str] = field(default_factory=set)


def sample() -> Checkpoint:
    return Checkpoint(
        "REQ-1", State.WORKING, Decimal("1234.50"), {"B2", "A1"}, date(2026, 10, 9),
        Step(Result.MET, datetime(2026, 10, 9, 14, 30, 5, 123456, tzinfo=timezone.utc), {"status": "not_needed"}),
        {"agent-a": 2}, [State.INTAKE, State.WORKING], (date(2026, 1, 1), date(2026, 12, 31)))


def with_state(cp: Checkpoint, s: State) -> Checkpoint:
    out = copy.deepcopy(cp)
    out.state = s
    return out


class ConditionalWrites(unittest.TestCase):
    def test_create_if_absent(self):
        table = FakeTable("request_id")
        self.assertEqual(st.CheckpointStore(table, Checkpoint).save(sample()), 1)
        with self.assertRaises(st.StaleCheckpointError) as ctx:
            st.CheckpointStore(table, Checkpoint).save(with_state(sample(), State.PEND))
        self.assertIn("create", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, FakeClientError)
        loaded = st.CheckpointStore(table, Checkpoint).load("REQ-1")
        self.assertEqual((loaded.version, loaded.checkpoint.state), (1, State.WORKING))  # first write kept

    def test_second_writer_on_the_same_version_loses(self):
        table = FakeTable("request_id")
        st.CheckpointStore(table, Checkpoint).save(sample())
        a, b = st.CheckpointStore(table, Checkpoint), st.CheckpointStore(table, Checkpoint)  # two processes
        (cp_a, v_a), (cp_b, v_b) = a.load("REQ-1"), b.load("REQ-1")
        self.assertEqual(a.save(with_state(cp_a, State.PEND), expected_version=v_a), 2)
        with self.assertRaises(st.StaleCheckpointError):
            b.save(with_state(cp_b, State.INTAKE), expected_version=v_b)
        cp, version = b.load("REQ-1")  # reload and re-decide: the winner's write is intact
        self.assertEqual((cp.state, version), (State.PEND, 2))
        self.assertEqual(b.save(with_state(cp, State.INTAKE), expected_version=version), 3)
        with self.assertRaises(st.StaleCheckpointError):
            b.save(cp, expected_version=7)  # a version nobody read
        self.assertEqual(set(table.consistent), {True})

    def test_eight_racing_writers_exactly_one_wins(self):
        table = FakeTable("request_id")
        st.CheckpointStore(table, Checkpoint).save(sample())
        barrier, outcomes = threading.Barrier(8), []

        def writer(n: int) -> None:
            store = st.CheckpointStore(table, Checkpoint)
            cp, version = store.load("REQ-1")
            cp.attempts = {"writer": n}
            barrier.wait()  # everyone has read version 1 before anyone writes
            try:
                store.save(cp, expected_version=version)
                outcomes.append(("won", n))
            except st.StaleCheckpointError:
                outcomes.append(("stale", n))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [n for kind, n in outcomes if kind == "won"]
        self.assertEqual((len(winners), len(outcomes)), (1, 8), outcomes)
        cp, version = st.CheckpointStore(table, Checkpoint).load("REQ-1")
        self.assertEqual((version, cp.attempts), (2, {"writer": winners[0]}))

    def test_lost_response_retry_is_success(self):
        class RetryingTable(FakeTable):
            """The write lands, the response is lost, botocore resends the same put and it fails the condition."""
            def put_item(self, **kw):
                super().put_item(**kw)
                return super().put_item(**kw)
        table = RetryingTable("request_id")
        store = st.CheckpointStore(table, Checkpoint)
        self.assertEqual(store.save(sample()), 1)
        cp, v = store.load("REQ-1")
        self.assertEqual(store.save(with_state(cp, State.PEND), expected_version=v), 2)
        self.assertEqual(store.load("REQ-1").checkpoint.state, State.PEND)
        other = FakeTable("request_id")  # a different writer's row (other write_id) is still stale
        st.CheckpointStore(other, Checkpoint).save(sample())
        with self.assertRaises(st.StaleCheckpointError):
            st.CheckpointStore(other, Checkpoint).save(sample())

    def test_throttling_is_not_mistaken_for_a_conflict(self):
        class Throttled(FakeTable):
            def put_item(self, **_):
                raise FakeClientError("ProvisionedThroughputExceededException")
        with self.assertRaises(FakeClientError):
            st.CheckpointStore(Throttled("request_id"), Checkpoint).save(sample())


class TypedCheckpoints(unittest.TestCase):
    @unittest.skipUnless(HAVE_BOTO3, "needs boto3's DynamoDB type serializer")
    def test_types_round_trip_in_a_fresh_process(self):
        table = FakeTable("request_id")
        st.CheckpointStore(table, Checkpoint).save(sample())
        wire = json.dumps({k: TypeSerializer().serialize(row)["M"] for k, row in table.rows.items()})
        out = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--load-from-wire"],
                             input=wire, capture_output=True, text=True, timeout=120)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "round-trip ok, version 1"), out.stderr)

    def test_values_that_would_not_round_trip_are_refused_at_save(self):
        table = FakeTable("request_id")
        store = st.CheckpointStore(table, Checkpoint)
        for bad in ({"amount": Decimal("1.10")}, {"result": Result.MET}, {"at": datetime.now(timezone.utc)}, {1: "x"}):
            cp = sample()
            cp.step.notes = bad  # an untyped dict
            with self.assertRaises(TypeError):
                store.save(cp)
        cp = sample()
        cp.amount = 1234.5  # a float in a Decimal field
        with self.assertRaises(TypeError):
            store.save(cp)
        self.assertEqual(table.rows, {})  # nothing half-written

    def test_schema_version_mismatch_and_legacy_rows_refused(self):
        table = FakeTable("request_id")
        st.CheckpointStore(table, Checkpoint, schema_version=2).save(sample())  # newer code wrote it
        with self.assertRaises(st.CheckpointSchemaError) as ctx:
            st.CheckpointStore(table, Checkpoint).load("REQ-1")
        self.assertIn("schema_version is 2", str(ctx.exception))
        legacy = FakeTable("request_id")
        legacy.rows["REQ-1"] = {"request_id": "REQ-1", "state": "WORKING",
                                "checkpoint_json": json.dumps({"request_id": "REQ-1", "state": "WORKING"})}
        with self.assertRaises(st.CheckpointSchemaError) as ctx:
            st.CheckpointStore(legacy, Checkpoint).load("REQ-1")
        self.assertIn("None", str(ctx.exception))

    def test_tampered_rows_refused(self):
        cases = {
            "unknown field": lambda b: b.update(appeal_note="x"),
            "unknown enum value": lambda b: b["step"].update(result="maybe"),
            "bad decimal": lambda b: b.update(amount="12,50"),
            "missing required field": lambda b: b.pop("amount"),
            "wrong scalar type": lambda b: b["attempts"].update({"agent-a": "2"}),
        }
        for name, edit in cases.items():
            with self.subTest(name):
                table = FakeTable("request_id")
                st.CheckpointStore(table, Checkpoint).save(sample())
                body = json.loads(table.rows["REQ-1"]["checkpoint_json"])
                edit(body)
                table.rows["REQ-1"]["checkpoint_json"] = json.dumps(body)
                with self.assertRaises(st.CheckpointSchemaError):
                    st.CheckpointStore(table, Checkpoint).load("REQ-1")


class CodecEdges(unittest.TestCase):
    def test_init_false_field_round_trips(self):
        table = FakeTable("request_id")
        store = st.CheckpointStore(table, WithDerived)
        cp = WithDerived("R")
        cp.derived = 7
        store.save(cp)
        self.assertEqual(store.load("R").checkpoint.derived, 7)

    def test_none_refused_for_non_optional_on_save_and_load(self):
        table = FakeTable("request_id")
        store = st.CheckpointStore(table, Strict)
        with self.assertRaises(TypeError):
            store.save(Strict("R", None))
        store.save(Strict("R", 1))
        body = json.loads(table.rows["R"]["checkpoint_json"])
        body["count"] = None
        table.rows["R"]["checkpoint_json"] = json.dumps(body)
        with self.assertRaises(st.CheckpointSchemaError):
            store.load("R")

    def test_set_needs_a_list(self):
        with self.assertRaises(TypeError):
            st.from_jsonable(set[str], "abc")

    def test_condition_failed_tolerates_odd_response(self):
        e = Exception("x")
        e.response = "not a dict"
        self.assertFalse(st._condition_failed(e))


class Registry(unittest.TestCase):
    def test_version_comparison_pads_and_skips_unparsable(self):
        self.assertTrue(st._version_at_least("1", "1.0"))
        self.assertTrue(st._version_at_least("1.0.0", "1"))
        self.assertFalse(st._version_at_least("1", "1.1"))
        cards = FakeTable("agent_name")
        reg = st.AgentCardRegistry(cards)
        reg.register(st.AgentCard("beta", ["x"], version="1.0-beta"), lambda: 1)
        reg.register(st.AgentCard("good", ["x"], version="2"), lambda: 1)
        with self.assertLogs("dynamodb_state", level="WARNING"):
            self.assertEqual(reg.discover_by_capability("x", min_version="1.0").card.agent_name, "good")

    def test_registry_survives_a_new_process(self):
        cards = FakeTable("agent_name")
        st.AgentCardRegistry(cards).register(st.AgentCard("policy-agent", ["policy_lookup"]), lambda: "called")
        fresh = st.AgentCardRegistry(cards)  # a "new process": same table, no local bindings
        self.assertTrue(fresh.is_available("policy-agent"))
        self.assertEqual(fresh.discover_by_capability("policy_lookup").card.agent_name, "policy-agent")
        with self.assertRaises(st.AgentNotFoundError):
            fresh.discover_by_capability("policy_lookup").invoke_fn()  # registered, but not callable here
        with self.assertRaises(st.AgentVersionMismatchError):
            fresh.discover_by_capability("policy_lookup", min_version="2.0")
        with self.assertRaises(st.AgentNotFoundError):
            fresh.discover_by_capability("billing")
        self.assertEqual(set(cards.consistent), {True})


def _load_from_wire() -> None:
    """Child process: rebuild the table from DynamoDB JSON, load, and compare field by field with types."""
    table = FakeTable("request_id")
    table.rows = {k: {a: TypeDeserializer().deserialize(v) for a, v in row.items()}
                  for k, row in json.loads(sys.stdin.read()).items()}
    back, version = st.CheckpointStore(table, Checkpoint).load("REQ-1")
    want = sample()
    assert back == want, (back, want)
    assert back.state is State.WORKING and back.step.result is Result.MET  # enums, not strings
    assert type(back.amount) is Decimal and str(back.amount) == "1234.50"
    assert type(back.codes) is set and type(back.received) is date
    assert back.step.decided_at.tzinfo is not None and back.step.decided_at.microsecond == 123456
    assert all(s.__class__ is State for s in back.history) and type(back.window) is tuple
    assert type(back.attempts["agent-a"]) is int
    print(f"round-trip ok, version {version}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--load-from-wire"]:
        _load_from_wire()
    else:
        unittest.main()

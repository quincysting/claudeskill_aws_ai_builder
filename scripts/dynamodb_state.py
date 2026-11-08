"""DynamoDB-backed agent-card registry and checkpoint store for a multi-agent orchestrator.

- Registry: one item per agent (PK `agent_name`); discovery by capability scans the table with a consistent
  read, so a card registered a moment ago is visible. `invoke_fn` is a live callable and can't be stored,
  so it's bound per process.
- Checkpoints: one item per request (PK `request_id`). Writes are conditional, so two writers on one case
  (a retry and a resume, two replicas) can't silently overwrite each other:
      v = store.save(cp)                      # create: fails if the request already has a checkpoint
      cp, v = store.load(request_id)          # (checkpoint, version), or None
      v = store.save(cp, expected_version=v)  # update: fails unless nobody wrote since you read v
  Both failures raise StaleCheckpointError: reload and decide again, never blind-overwrite.
  Each save stores a random write_id. If a write succeeded but its response was lost, botocore retries it and
  the retry fails the condition; the failure returns the stored item (ALL_OLD), and a matching write_id means
  the write is ours and save() returns success.
- The checkpoint body is JSON encoded through the dataclass's type hints: enums, nested dataclasses,
  datetime/date, Decimal, sets, tuples and typed lists/dicts round-trip; a value that wouldn't (a Decimal or
  enum inside an untyped dict) is refused at save time. Each item carries SCHEMA_VERSION, and load() refuses
  another version, unknown fields or undecodable values with CheckpointSchemaError.

Table contract: checkpoints PK `request_id` (S), and the checkpoint dataclass has a `request_id` field;
registry PK `agent_name` (S). Real use: `CheckpointStore(dynamodb_table("CheckpointTable"), Checkpoint)`.
Python 3.10+ (it decodes `X | None` hints). Standard library, plus boto3 only in dynamodb_table().
Tests: test_dynamodb_state.py, offline (a fake table that honours ConditionExpression), and
test_real_libraries.py, which runs the same writes through boto3 into moto's DynamoDB.
"""
from __future__ import annotations

import json
import logging
import types
import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, NamedTuple, Union, get_args, get_origin, get_type_hints

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1  # bump on ANY change to the checkpoint dataclasses; older code then refuses the new rows


def dynamodb_table(name: str) -> Any:
    import boto3  # lazy, so the tests run without AWS

    return boto3.resource("dynamodb").Table(name)


class AgentNotFoundError(Exception):
    pass


class AgentVersionMismatchError(Exception):
    pass


class StaleCheckpointError(Exception):
    """Another writer created or advanced this checkpoint since you read it. Reload and re-decide."""


class CheckpointSchemaError(Exception):
    """The stored checkpoint can't be read by this code. Don't resume it: migrate it or restart the case."""


@dataclass
class AgentCard:
    agent_name: str
    capabilities: list[str]
    input_schema: dict[str, str] = field(default_factory=dict)
    output_schema: dict[str, str] = field(default_factory=dict)
    version: str = "1.0"


@dataclass
class RegisteredAgent:
    card: AgentCard
    invoke_fn: Callable


class AgentCardRegistry:
    def __init__(self, table: Any) -> None:
        self._table = table
        self._invoke_fns: dict[str, Callable] = {}

    def register(self, card: AgentCard, invoke_fn: Callable) -> None:
        self._table.put_item(Item=asdict(card))  # a card is an idempotent upsert, not run state
        self._invoke_fns[card.agent_name] = invoke_fn

    def discover_by_capability(self, capability: str, min_version: str = "1.0") -> RegisteredAgent:
        candidates = [c for c in self._scan() if capability in c.capabilities]
        if not candidates:
            raise AgentNotFoundError(f"No registered agent advertises capability '{capability}'")
        candidates.sort(key=lambda c: c.agent_name not in self._invoke_fns)  # callable here first
        _version_tuple(min_version)  # a bad min_version is the caller's error: let it raise
        for card in candidates:
            try:
                ok = _version_at_least(card.version, min_version)
            except ValueError:
                _logger.warning("skipping agent %r: unparsable version %r", card.agent_name, card.version)
                continue
            if ok:
                return RegisteredAgent(card, self._invoke_fns.get(card.agent_name, _unbound(card.agent_name)))
        raise AgentVersionMismatchError(
            f"'{capability}' found, but no version >= {min_version}: {[c.version for c in candidates]}"
        )

    def is_available(self, agent_name: str) -> bool:
        return "Item" in self._table.get_item(Key={"agent_name": agent_name}, ConsistentRead=True)

    def get_card(self, agent_name: str) -> AgentCard:
        item = self._table.get_item(Key={"agent_name": agent_name}, ConsistentRead=True).get("Item")
        if item is None:
            raise AgentNotFoundError(f"No registered agent named '{agent_name}'")
        return _card(item)

    def _scan(self) -> list[AgentCard]:
        items: list[dict] = []
        kwargs: dict[str, Any] = {"ConsistentRead": True}  # Scan is eventually consistent by default
        while True:  # Scan pages at 1 MB
            page = self._table.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return [_card(i) for i in items]
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _card(item: dict) -> AgentCard:
    return AgentCard(
        agent_name=item["agent_name"],
        capabilities=list(item.get("capabilities", [])),
        input_schema=dict(item.get("input_schema", {})),
        output_schema=dict(item.get("output_schema", {})),
        version=item.get("version", "1.0"),
    )


def _unbound(agent_name: str) -> Callable:
    def invoke(*_: Any, **__: Any) -> Any:
        raise AgentNotFoundError(f"'{agent_name}' is registered but has no invoke binding in this process")

    return invoke


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _version_at_least(actual: str, minimum: str) -> bool:
    a, m = _version_tuple(actual), _version_tuple(minimum)
    width = max(len(a), len(m))  # "1" == "1.0"
    return a + (0,) * (width - len(a)) >= m + (0,) * (width - len(m))


class Loaded(NamedTuple):
    checkpoint: Any
    version: int  # pass back as save(..., expected_version=version)


class CheckpointStore:
    def __init__(self, table: Any, checkpoint_cls: type, schema_version: int = SCHEMA_VERSION) -> None:
        self._table, self._cls, self._schema = table, checkpoint_cls, schema_version

    def save(self, checkpoint: Any, expected_version: int = 0) -> int:
        """Create (expected_version=0) or update from the version you loaded. Returns the new version."""
        body = to_jsonable(self._cls, checkpoint)  # refuses what wouldn't round-trip, before anything is written
        write_id = uuid.uuid4().hex
        item = {"request_id": checkpoint.request_id, "state": body.get("state"), "version": expected_version + 1,
                "schema_version": self._schema, "checkpoint_json": json.dumps(body), "write_id": write_id}
        if expected_version == 0:
            condition: dict[str, Any] = {"ConditionExpression": "attribute_not_exists(request_id)"}
        else:
            condition = {"ConditionExpression": "#v = :expected", "ExpressionAttributeNames": {"#v": "version"},
                         "ExpressionAttributeValues": {":expected": expected_version}}
        try:
            self._table.put_item(Item=item, ReturnValuesOnConditionCheckFailure="ALL_OLD", **condition)
        except Exception as exc:
            if _condition_failed(exc):
                if _stored_write_id(exc) == write_id:
                    return expected_version + 1  # our own write landed; this was botocore's retry
                seen = f"version {expected_version}" if expected_version else "no checkpoint (create)"
                raise StaleCheckpointError(f"{checkpoint.request_id}: you read {seen}, but another writer got "
                                           "there first; reload and re-decide") from exc
            raise
        return expected_version + 1

    def load(self, request_id: str) -> Loaded | None:
        item = self._table.get_item(Key={"request_id": request_id}, ConsistentRead=True).get("Item")
        if item is None:
            return None
        stored = item.get("schema_version")
        if stored != self._schema:  # DynamoDB returns Decimal(1), which == 1
            raise CheckpointSchemaError(
                f"{request_id}: checkpoint schema_version is {stored} (None = written before versioning), this "
                f"code reads {self._schema}. Migrate it or restart the case deliberately; don't resume it.")
        try:
            return Loaded(from_jsonable(self._cls, json.loads(item["checkpoint_json"]), strict=True),
                          int(item["version"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise CheckpointSchemaError(f"{request_id}: stored checkpoint doesn't decode as "
                                        f"{self._cls.__name__}: {exc}") from exc


def _condition_failed(exc: Exception) -> bool:
    """botocore ClientError for a failed ConditionExpression (checked by code, so botocore isn't imported)."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"


def _stored_write_id(exc: Exception) -> Any:
    """write_id of the item returned by ReturnValuesOnConditionCheckFailure (low-level {"S": ...} or plain)."""
    item = exc.response.get("Item")  # type: ignore[attr-defined]
    wid = item.get("write_id") if isinstance(item, dict) else None
    return wid.get("S") if isinstance(wid, dict) else wid


# ---------- typed JSON codec: the dataclass's type hints are the schema ----------

def to_jsonable(tp: Any, value: Any, path: str = "$") -> Any:
    """Encode `value`, declared as `tp`, as plain JSON data. TypeError if it wouldn't decode back the same."""
    if value is None:
        return _none_ok(tp, path)
    tp = _unwrap_optional(tp, path)
    origin, args = get_origin(tp) or tp, get_args(tp)
    if is_dataclass(tp):
        _expect(value, tp, path)
        hints = get_type_hints(tp)
        return {f.name: to_jsonable(hints[f.name], getattr(value, f.name), f"{path}.{f.name}") for f in fields(tp)}
    if isinstance(tp, type) and issubclass(tp, Enum):
        _expect(value, tp, path)
        return _plain(value.value, path)
    if tp in (datetime, date):
        _expect(value, tp, path)
        if tp is date and isinstance(value, datetime):
            raise TypeError(f"{path}: a datetime in a date field would lose its time")
        return value.isoformat()
    if tp is Decimal:
        _expect(value, Decimal, path)
        return str(value)  # keeps precision and trailing zeros: "1234.50"
    if origin in (set, frozenset):
        _expect(value, origin, path)
        return sorted((to_jsonable(args[0] if args else Any, v, f"{path}[]") for v in value), key=json.dumps)
    if origin in (list, tuple):
        _expect(value, origin, path)
        item_types = _item_types(origin, args, value, path)
        return [to_jsonable(t, v, f"{path}[{i}]") for i, (t, v) in enumerate(zip(item_types, value))]
    if origin is dict:
        _expect(value, dict, path)
        key_tp, val_tp = args or (Any, Any)
        out = {}
        for k, v in value.items():
            key = to_jsonable(key_tp, k, f"{path}{{key}}")
            if not isinstance(key, str):
                raise TypeError(f"{path}: JSON object keys must encode as strings, got {type(k).__name__}")
            out[key] = to_jsonable(val_tp, v, f"{path}.{key}")
        return out
    if tp in (str, int, float, bool):
        _expect(value, (int, float) if tp is float else tp, path)
        return value
    return _plain(value, path)  # Any, bare containers, unsupported types: plain JSON only


def from_jsonable(tp: Any, data: Any, path: str = "$", *, strict: bool = False) -> Any:
    """Rebuild a value from JSON data using its declared type. strict=True refuses fields the dataclass
    doesn't declare: for state you read, modify and write back, a dropped field would be lost for good."""
    if data is None:
        return _none_ok(tp, path)
    tp = _unwrap_optional(tp, path)
    origin, args = get_origin(tp) or tp, get_args(tp)
    if is_dataclass(tp):
        _expect(data, dict, path)
        hints, names = get_type_hints(tp), [f.name for f in fields(tp)]
        unknown = set(data) - set(names)
        if strict and unknown:
            raise TypeError(f"{path}: unknown fields {sorted(unknown)} (written by newer code?)")
        built = {n: from_jsonable(hints[n], data[n], f"{path}.{n}", strict=strict) for n in names if n in data}
        init_names = {f.name for f in fields(tp) if f.init}
        obj = tp(**{n: v for n, v in built.items() if n in init_names})
        for n, v in built.items():
            if n not in init_names:
                setattr(obj, n, v)  # init=False fields can't go through the constructor
        return obj
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(data)
    if tp in (datetime, date, Decimal):
        _expect(data, str, path)
        return Decimal(data) if tp is Decimal else tp.fromisoformat(data)
    if origin in (set, frozenset):
        _expect(data, list, path)
        return origin(from_jsonable(args[0] if args else Any, v, f"{path}[]", strict=strict) for v in data)
    if origin in (list, tuple):
        _expect(data, list, path)
        return origin(from_jsonable(t, v, f"{path}[{i}]", strict=strict)
                      for i, (t, v) in enumerate(zip(_item_types(origin, args, data, path), data)))
    if origin is dict:
        _expect(data, dict, path)
        key_tp, val_tp = args or (Any, Any)
        return {from_jsonable(key_tp, k, path): from_jsonable(val_tp, v, f"{path}.{k}", strict=strict)
                for k, v in data.items()}
    if tp in (str, int, float, bool):
        _expect(data, (int, float) if tp is float else tp, path)
        return float(data) if tp is float else data
    return data


def _none_ok(tp: Any, path: str) -> None:
    """None is valid only for Optional, Any or None-typed fields."""
    if tp is Any or tp is object or tp is None or tp is type(None):
        return None
    if get_origin(tp) in (Union, types.UnionType) and (type(None) in get_args(tp) or Any in get_args(tp)):
        return None
    raise TypeError(f"{path}: None is not allowed for {getattr(tp, '__name__', tp)}; declare it X | None")


def _unwrap_optional(tp: Any, path: str) -> Any:
    if get_origin(tp) in (Union, types.UnionType):
        options = [a for a in get_args(tp) if a is not type(None)]
        if len(options) != 1:
            raise TypeError(f"{path}: {tp} can't be decoded unambiguously; use one type, or X | None")
        return options[0]
    return tp


def _item_types(origin: Any, args: tuple, value: Any, path: str) -> list:
    if origin is tuple and args and args[-1] is not Ellipsis:  # tuple[A, B]: fixed length
        if len(args) != len(value):
            raise TypeError(f"{path}: expected {len(args)} items, got {len(value)}")
        return list(args)
    return [args[0] if args else Any] * len(value)


def _expect(value: Any, tp: Any, path: str) -> None:
    if not isinstance(value, tp):
        raise TypeError(f"{path}: expected {getattr(tp, '__name__', tp)}, got {type(value).__name__}")


def _plain(value: Any, path: str) -> Any:
    """Untyped data must already be plain JSON; anything else would come back as a different type."""
    if value is None or type(value) in (str, int, float, bool):  # exact: a str-Enum or Decimal is refused
        return value
    if type(value) is list:
        return [_plain(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if type(value) is dict and all(type(k) is str for k in value):
        return {k: _plain(v, f"{path}.{k}") for k, v in value.items()}
    raise TypeError(f"{path}: {type(value).__name__} in an untyped field won't round-trip through JSON; "
                    "declare the field's type")

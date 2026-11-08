"""The same scripts against real libraries instead of hand-written fakes. Offline: no network, no AWS.

- dynamodb_state and jwt_authorizer's audit writer run through real boto3 and botocore (which validates each
  request against the DynamoDB service model) into moto's DynamoDB, which evaluates the condition
  expressions and ReturnValuesOnConditionCheckFailure itself.
- jwt_authorizer.verify_rs256 is compared with PyJWT on the same valid and tampered tokens.
- genai_telemetry's attribute names are compared with opentelemetry-semantic-conventions and set on a real
  SDK span.
- policy_smoke_test's request and the replies it classifies are parsed with the MCP SDK's JSON-RPC types.

moto is still a simulation of DynamoDB, so this narrows the gap to live AWS without closing it.
Each class skips when its library is missing. Run: python -B -m unittest test_real_libraries
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import random
import time
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from unittest import mock


def _have(*mods: str) -> bool:
    try:
        for m in mods:
            __import__(m)
        return True
    except ImportError:
        return False


HAVE_MOTO = _have("boto3", "moto")
HAVE_PYJWT = _have("jwt", "cryptography")
HAVE_OTEL = _have("opentelemetry.sdk.trace", "opentelemetry.semconv._incubating.attributes.gen_ai_attributes")
HAVE_MCP = _have("mcp.types")

FAKE_AWS = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
            "AWS_DEFAULT_REGION": "us-east-1"}


class Stage(str, Enum):
    TRIAGE = "triage"
    APPROVED = "approved"


@dataclass
class Case:
    request_id: str
    state: Stage
    amount: Decimal
    tags: set[str]
    opened_at: datetime
    steps: list[tuple[str, int]] = field(default_factory=list)
    note: str | None = None


def _case(**over) -> Case:
    base = dict(request_id="req-1", state=Stage.TRIAGE, amount=Decimal("1234.50"), tags={"b", "a"},
                opened_at=datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc), steps=[("triage", 1)])
    return Case(**{**base, **over})


@unittest.skipUnless(HAVE_MOTO, "needs boto3 and moto")
class DynamoDBOnMoto(unittest.TestCase):
    def setUp(self):
        import boto3
        from moto import mock_aws

        env = mock.patch.dict(os.environ, FAKE_AWS)
        env.start()
        self.addCleanup(env.stop)
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        self.ddb = boto3.resource("dynamodb")

    def table(self, name: str, pk: str, sk: str | None = None):
        keys = [{"AttributeName": pk, "KeyType": "HASH"}] + ([{"AttributeName": sk, "KeyType": "RANGE"}] if sk else [])
        attrs = [{"AttributeName": a, "AttributeType": "S"} for a in (pk, sk) if a]
        return self.ddb.create_table(TableName=name, KeySchema=keys, AttributeDefinitions=attrs,
                                     BillingMode="PAY_PER_REQUEST")

    def test_versioned_writes(self):
        import dynamodb_state as ds

        store = ds.CheckpointStore(self.table("Checkpoints", "request_id"), Case)
        self.assertEqual(store.save(_case()), 1)
        with self.assertRaises(ds.StaleCheckpointError):
            store.save(_case())  # a second create loses
        cp, v = store.load("req-1")
        self.assertEqual((cp, v), (_case(), 1))  # Decimal, set, enum, datetime and tuples survive DynamoDB
        cp.state = Stage.APPROVED
        self.assertEqual(store.save(cp, expected_version=v), 2)
        with self.assertRaises(ds.StaleCheckpointError):
            store.save(cp, expected_version=v)  # a writer still holding version 1 loses
        self.assertEqual(store.load("req-1").checkpoint.state, Stage.APPROVED)
        self.assertIsNone(store.load("missing"))

    def test_lost_response_retry_counts_as_success(self):
        """The write lands, its response is lost, and the SDK sends it again: the retry fails the condition and
        moto returns the stored item (ALL_OLD), whose write_id is ours."""
        import dynamodb_state as ds

        real = self.table("Checkpoints", "request_id")

        class LostResponse:
            def __init__(self):
                self.sends = 0

            def put_item(self, **kw):
                self.sends += 1
                real.put_item(**kw)  # lands
                self.sends += 1
                real.put_item(**kw)  # the retry: raises ConditionalCheckFailedException

            def get_item(self, **kw):
                return real.get_item(**kw)

        lossy = LostResponse()
        store = ds.CheckpointStore(lossy, Case)
        self.assertEqual(store.save(_case()), 1)
        self.assertEqual(store.save(_case(note="x"), expected_version=1), 2)
        self.assertEqual(lossy.sends, 4)
        # a different writer's item does not count as ours
        with self.assertRaises(ds.StaleCheckpointError):
            ds.CheckpointStore(real, Case).save(_case(), expected_version=1)

    def test_registry_discovery(self):
        import dynamodb_state as ds

        reg = ds.AgentCardRegistry(self.table("Agents", "agent_name"))
        reg.register(ds.AgentCard("billing", ["refunds"], version="1.2"), lambda: "billing")
        reg.register(ds.AgentCard("billing-old", ["refunds"], version="1.0"), lambda: "old")
        self.assertEqual(reg.discover_by_capability("refunds", min_version="1.1").card.agent_name, "billing")
        self.assertTrue(reg.is_available("billing"))
        self.assertEqual(reg.get_card("billing").capabilities, ["refunds"])
        with self.assertRaises(ds.AgentNotFoundError):
            reg.discover_by_capability("payroll")

    def test_authorizer_audit_row(self):
        import jwt_authorizer as authz

        authz._ddb_audit.cache_clear()
        self.addCleanup(authz._ddb_audit.cache_clear)
        table = self.table("AuthzAudit", "trace_id", "seq")
        write = authz._ddb_audit("AuthzAudit")
        write({"event": "authz_decision", "resource": "accounts-api", "result": "deny", "reason": "wrong_audience",
               "trace_id": "t-0001", "aud": ["a", "b"]})
        row = table.query(KeyConditionExpression="trace_id = :t",
                          ExpressionAttributeValues={":t": "t-0001"})["Items"][0]
        self.assertTrue(row["seq"].endswith("#accounts-api#deny"))
        self.assertEqual((row["reason"], json.loads(row["aud"])), ("wrong_audience", ["a", "b"]))


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _rsa_sign(key, signing_input: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    return key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())


@unittest.skipUnless(HAVE_PYJWT, "needs PyJWT and cryptography")
class VerifierAgreesWithPyJWT(unittest.TestCase):
    """verify_rs256 and PyJWT accept and refuse the same tokens (signature only; claims are decide()'s job)."""

    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.jwt = jwt
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = cls.key.public_key().public_numbers()
        cls.n = pub.n
        cls.jwk = {"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                   "n": _b64u(pub.n.to_bytes(256, "big")), "e": _b64u(pub.e.to_bytes(3, "big"))}
        now = int(time.time())
        cls.claims = {"iss": "https://idp.example.com", "aud": "api", "sub": "u-1", "iat": now, "exp": now + 300}
        cls.good = jwt.encode(cls.claims, cls.key, algorithm="RS256", headers={"kid": "k1"})

    def verdicts(self, tok: str, jwk: dict | None = None) -> tuple[bool, bool]:
        import jwt_authorizer as authz

        jwk = jwk or self.jwk
        ours = authz.verify_rs256(tok, [jwk])[1] is not None
        try:
            self.jwt.decode(tok, self.jwt.PyJWK(jwk).key, algorithms=["RS256"],
                            options={"verify_signature": True, "verify_exp": False, "verify_iat": False,
                                     "verify_aud": False, "verify_iss": False})
            theirs = True
        except self.jwt.PyJWTError:
            theirs = False
        return ours, theirs

    def test_named_attacks(self):
        h, p, s = self.good.split(".")
        sig = int.from_bytes(base64.urlsafe_b64decode(s + "=="), "big")
        plus_n = sig + self.n
        hs_header = _b64u(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
        hs_sig = hmac.new(json.dumps(self.jwk).encode(), f"{hs_header}.{p}".encode(), hashlib.sha256).digest()
        rsa_under_hs_header = _rsa_sign(self.key, f"{hs_header}.{p}".encode())  # a valid RSA signature, lying alg
        zero_padded = _b64u(bytes(1) + sig.to_bytes(256, "big"))  # same integer, one byte too long
        cases = {
            "alg_header_lies": (f"{hs_header}.{p}.{_b64u(rsa_under_hs_header)}", False),
            "signature_zero_padded": (f"{h}.{p}.{zero_padded}", False),
            "valid": (self.good, True),
            "other_key": (self.jwt.encode(self.claims, self.other, algorithm="RS256", headers={"kid": "k1"}), False),
            "payload_swapped": (f"{h}.{_b64u(json.dumps({**self.claims, 'sub': 'admin'}).encode())}.{s}", False),
            "alg_none": (f"{_b64u(json.dumps({'alg': 'none', 'kid': 'k1'}).encode())}.{p}.", False),
            "alg_hs256_confusion": (f"{hs_header}.{p}.{_b64u(hs_sig)}", False),
            "signature_plus_n": (f"{h}.{p}.{_b64u(plus_n.to_bytes((plus_n.bit_length() + 7) // 8, 'big'))}", False),
            "signature_truncated": (f"{h}.{p}.{s[:-6]}", False),
            "no_signature": (f"{h}.{p}", False),
        }
        for label, (tok, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.verdicts(tok), (expected, expected))

    def test_signature_plus_n_at_the_same_length(self):
        """s + n that still fits the key size: only the s < n check (RFC 8017 RSAVP1) refuses it."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        for _ in range(60):  # a modulus well below 2**2048 leaves room for s + n in 256 bytes
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            n = key.public_key().public_numbers().n
            if n < 3 << 2045:
                break
        jwk = {"kty": "RSA", "kid": "k2", "n": _b64u(n.to_bytes(256, "big")), "e": _b64u((65537).to_bytes(3, "big"))}
        for i in range(64):
            tok = self.jwt.encode({**self.claims, "jti": str(i)}, key, algorithm="RS256", headers={"kid": "k2"})
            h, p, s = tok.split(".")
            sig = int.from_bytes(base64.urlsafe_b64decode(s + "=="), "big")
            if sig + n < 1 << 2048:
                break
        self.assertLess(sig + n, 1 << 2048, "no token left room for s + n; rerun")
        self.assertEqual(self.verdicts(tok, jwk), (True, True))
        forged = f"{h}.{p}.{_b64u((sig + n).to_bytes(256, 'big'))}"
        self.assertEqual(self.verdicts(forged, jwk), (False, False))

    def test_random_mutations(self):
        rng = random.Random(20261108)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.=!"
        for i in range(300):
            chars = list(self.good)
            pos = rng.randrange(len(chars))
            chars[pos] = rng.choice(alphabet.replace(chars[pos], ""))
            tok = "".join(chars)
            with self.subTest(i=i, pos=pos):
                ours, theirs = self.verdicts(tok)
                self.assertEqual(ours, theirs, tok)

    def test_weak_key_refused_by_ours(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        import jwt_authorizer as authz

        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        pub = weak.public_key().public_numbers()
        jwk = {"kty": "RSA", "kid": "w", "n": _b64u(pub.n.to_bytes(128, "big")), "e": _b64u(pub.e.to_bytes(3, "big"))}
        tok = self.jwt.encode(self.claims, weak, algorithm="RS256", headers={"kid": "w"})
        self.assertEqual(authz.verify_rs256(tok, [jwk])[2], "weak_key")  # stricter on purpose (MIN_RSA_BITS)


@unittest.skipUnless(HAVE_OTEL, "needs opentelemetry-sdk and opentelemetry-semantic-conventions")
class TelemetryOnRealOTel(unittest.TestCase):
    def test_names_match_semconv(self):
        from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as g
        import genai_telemetry as gt

        self.assertEqual(gt.INPUT, g.GEN_AI_USAGE_INPUT_TOKENS)
        self.assertEqual(gt.OUTPUT, g.GEN_AI_USAGE_OUTPUT_TOKENS)
        # the cache counts are newer than some semconv releases (0.60b1 has neither); compare when present
        for const, ours in (("GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS", gt.CACHE_READ),
                            ("GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS", gt.CACHE_WRITE)):
            if hasattr(g, const):
                self.assertEqual(getattr(g, const), ours)

    def test_attributes_survive_a_real_span(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        import genai_telemetry as gt

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        attrs = gt.usage_attributes({"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 900,
                                     "cacheWriteInputTokens": 0})
        with provider.get_tracer("test").start_as_current_span("chat") as span:
            span.set_attributes(attrs)
        provider.shutdown()
        self.assertEqual(dict(exporter.get_finished_spans()[0].attributes), attrs)


@unittest.skipUnless(HAVE_MCP, "needs the mcp package")
class SmokeTestSpeaksMcp(unittest.TestCase):
    def test_request_is_a_valid_tools_call(self):
        import mcp.types as t
        import policy_smoke_test as pst

        sent: list[dict] = []
        ok_reply = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}}
        cases = [{"label": "read", "tool": "Orders___get_order", "arguments": {"order_id": "o-1"}, "expect": "allow"}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(pst.run("https://gw.example.com/mcp", cases, lambda u, p: sent.append(p) or ok_reply))
        req = t.JSONRPCRequest.model_validate(sent[0])
        call = t.ClientRequest.model_validate({"method": req.method, "params": req.params}).root
        self.assertIsInstance(call, t.CallToolRequest)
        self.assertEqual((call.params.name, call.params.arguments), ("Orders___get_order", {"order_id": "o-1"}))
        t.JSONRPCResponse.model_validate(ok_reply)

    def test_replies_classified(self):
        import mcp.types as t
        import policy_smoke_test as pst

        denial = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "Tool Execution Denied"}}
        bad_tool = {"jsonrpc": "2.0", "id": 1, "error": {"code": t.INVALID_PARAMS, "message": "unknown tool"}}
        tool_error = {"jsonrpc": "2.0", "id": 1,
                      "result": {"content": [{"type": "text", "text": "order not found"}], "isError": True}}
        t.JSONRPCError.model_validate(denial)
        t.JSONRPCError.model_validate(bad_tool)
        t.CallToolResult.model_validate(tool_error["result"])
        self.assertEqual([pst.decision(r) for r in (denial, bad_tool, tool_error)], ["deny", "error", "allow"])


if __name__ == "__main__":
    unittest.main()

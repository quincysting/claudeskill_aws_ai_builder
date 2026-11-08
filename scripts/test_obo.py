"""Offline tests for obo_exchange.py and jwt_authorizer.py with real RS256 tokens. No network, no AWS.

Run: python -B -m unittest test_obo
Needs PyJWT and cryptography to mint test tokens (test-only; both scripts are stdlib only). Without them the
whole module is skipped.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import time
import types
import unittest
import urllib.parse
from unittest import mock

try:
    import jwt  # PyJWT
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError as missing:  # pragma: no cover
    raise unittest.SkipTest(f"test_obo needs PyJWT and cryptography: {missing}")

import jwt_authorizer as authz
import obo_exchange as obo

ISS = "https://idp.example.com/realms/demo"
RES = "accounts-api"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NOW = int(time.time())


def _b64u(i: int) -> str:
    raw = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwk(key, kid: str) -> dict:
    pub = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig", "n": _b64u(pub.n), "e": _b64u(pub.e)}


JWKS = [_jwk(KEY, "k1")]


def token(key=KEY, kid="k1", hdr=None, **over) -> str:
    claims = {"iss": ISS, "aud": RES, "sub": "u-123", "preferred_username": "user-a", "typ": "Bearer",
              "subject_type": "human", "scope": "accounts.write email", "client_id": "payments-agent",
              "resource_access": {RES: {"roles": ["accounts.write"]}}, "iat": NOW - 10, "exp": NOW + 290}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid, **(hdr or {})})


def bearer(tok: str, **headers) -> dict:
    return {"headers": {"Authorization": f"Bearer {tok}", **headers}}


WRITE_POLICY = authz.Policy(issuer=ISS, resource=RES, required_scope="accounts.write",
                            actor_ceiling={"payments-agent": ["accounts.write"], "accounts-agent": ["accounts.read"]},
                            subject_types=["human"], max_lifetime=780)

CASES = {
    "allow": (token(), True, "ok"),
    "bad_signature": (token(key=OTHER_KEY), False, "bad_signature"),
    "unknown_kid": (token(kid="nope"), False, "unknown_kid"),
    "expired": (token(exp=NOW - 100, iat=NOW - 400), False, "token_expired"),
    "lifetime": (token(iat=NOW - 10, exp=NOW + 3600), False, "token_lifetime_too_long"),
    "issuer": (token(iss="https://evil.example.com"), False, "wrong_issuer"),
    "audience": (token(aud="payments-agent"), False, "wrong_audience"),
    "machine": (token(subject_type="service"), False, "subject_type_not_allowed"),
    "scope": (token(scope="accounts.read"), False, "insufficient_scope"),
    "actor": (token(client_id="advisor-agent"), False, "unknown_actor"),
    "ceiling": (token(client_id="accounts-agent"), False, "actor_over_ceiling"),
    "no_roles": (token(resource_access={}), False, "missing_role_grant"),
}

FAIL_CLOSED = {
    "missing_exp": (token(exp=None), "missing_exp"),
    "exp_not_a_number": (token(exp="soon"), "missing_exp"),
    "missing_iat": (token(iat=None), "missing_iat"),
    "iat_in_future": (token(iat=NOW + 120, exp=NOW + 300), "token_issued_in_future"),
    "nbf_in_future": (token(nbf=NOW + 120), "token_not_yet_valid"),
    "exp_before_iat": (token(iat=NOW - 10, exp=NOW - 10), "invalid_lifetime"),
    "missing_sub": (token(sub=None), "missing_sub"),
    "id_token_keycloak": (token(typ="ID"), "wrong_token_type"),
    "id_token_cognito": (token(typ=None, token_use="id"), "wrong_token_type"),
    "no_access_marker": (token(typ=None), "wrong_token_type"),
    "multi_audience": (token(aud=[RES, "payments-agent"]), "wrong_audience"),
    "missing_audience": (token(aud=None), "wrong_audience"),
    "missing_actor": (token(client_id=None), "unknown_actor"),
    "role_not_required_one": (token(resource_access={RES: {"roles": ["accounts.read"]}}), "missing_role_grant"),
    "alg_none": (jwt.encode({"iss": ISS, "aud": RES, "sub": "u-123"}, None, algorithm="none"), "unsupported_alg"),
    "alg_hs256": (jwt.encode({"iss": ISS}, "a-shared-secret-of-32-bytes-len!", algorithm="HS256",
                             headers={"kid": "k1"}), "unsupported_alg"),
    "garbage": ("x.y.z", "malformed_token"),
    "scope_wrong_type": (token(scope={"accounts.write": 1}), "insufficient_scope"),
}
ALSO_ALLOWED = {
    "rfc9068_at_jwt": token(typ=None, hdr={"typ": "at+jwt"}),
    "cognito_access": token(typ=None, token_use="access"),
}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def idp(new_token: str, seen: dict | None = None):
    """Fake IdP token endpoint returning `new_token`; records the request in `seen`."""
    def opener(req, timeout):
        if seen is not None:
            seen.update(url=req.full_url, body=urllib.parse.parse_qs(req.data.decode()),
                        ctype=req.headers["Content-type"])
        return Resp(json.dumps({"access_token": new_token}).encode())
    return opener


LOGIN = token(aud="advisor", client_id="advisor", scope="openid accounts.read email profile")


def hop_token(**over) -> str:
    """What the IdP returns for advisor -> accounts-agent."""
    return token(**{"aud": "accounts-agent", "client_id": "advisor", "scope": "accounts.read email profile", **over})


def run_delegate(new_token: str, requested=("accounts.read",), audit=None, seen=None):
    rows: list[dict] = []
    out = obo.delegate(LOGIN, "accounts-agent", list(requested), token_endpoint="https://idp.example.com/token",
                       client_id="advisor", client_secret="s", audit=audit or rows.append, trace_id="tr-1",
                       opener=idp(new_token, seen))
    return out, rows


class Quiet(unittest.TestCase):
    """The authorizer prints one JSON line per decision; keep test output clean."""

    def setUp(self):
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)


class TestObo(Quiet):
    def test_exchange_form_and_call(self):
        form = obo.exchange_form("SUBJ", "advisor-agent", "s3cr3t", "accounts-agent", ["accounts.read"])
        self.assertEqual(form["grant_type"], "urn:ietf:params:oauth:grant-type:token-exchange")
        self.assertEqual((form["audience"], form["scope"]), ("accounts-agent", "accounts.read aud-accounts-agent"))
        self.assertEqual(form["subject_token_type"], obo.ACCESS_TOKEN)
        self.assertEqual(form["requested_token_type"], obo.ACCESS_TOKEN)
        seen = {}
        self.assertEqual(obo.exchange_token("https://idp.example.com/token", form, opener=idp("NEW", seen)), "NEW")
        self.assertEqual(seen["body"]["subject_token"], ["SUBJ"])
        self.assertEqual(seen["ctype"], "application/x-www-form-urlencoded")
        self.assertEqual(obo.exchange_form("t", "c", "s", "x", ["a"], audience_scope_prefix=None)["scope"], "a")

    def test_malformed_token_response_is_a_refusal(self):
        def opener(req, timeout):
            return Resp(json.dumps({"error": "nope"}).encode())
        with self.assertRaises(PermissionError):
            obo.exchange_token("https://idp.example.com/token", {}, opener=opener)

    def test_plain_http_token_endpoint_refused(self):
        with self.assertRaises(ValueError):
            obo.exchange_token("http://idp.example.com/token", {}, opener=idp("NEW"))

    def test_bearer_and_hop_record(self):
        t = token()
        self.assertEqual(obo.bearer_from_headers({"AUTHORIZATION": f"Bearer {t}"}), t)
        with self.assertRaises(PermissionError):
            obo.bearer_from_headers({"x": "y"})
        rec = obo.hop_record("payments->accounts-api", t, 200)
        self.assertEqual(rec["sub_hash"], obo.subject_hash("u-123"))
        self.assertEqual((rec["aud"], rec["downstream_status"]), (RES, 200))
        dumped = json.dumps(rec)
        for secret in ("u-123", "user-a", t):
            self.assertNotIn(secret, dumped)
        self.assertEqual(obo.subject_hash("u-123"), authz.subject_hash("u-123"))  # rows join across hops

    def test_subject_hash_reads_key_at_call_time(self):
        with mock.patch.dict(os.environ, {"SUBJECT_HASH_KEY": "k1"}):
            first = obo.subject_hash("u-123")
        with mock.patch.dict(os.environ, {"SUBJECT_HASH_KEY": "k2"}):
            self.assertNotEqual(obo.subject_hash("u-123"), first)
            self.assertEqual(authz.subject_hash("u-123"), obo.subject_hash("u-123"))

    def test_check_chain(self):
        good = [{"hop": "advisor->accounts-agent", "sub_hash": "h", "aud": "accounts-agent",
                 "scope": "accounts.read email"},
                {"hop": "accounts->accounts-api", "sub_hash": "h", "aud": "accounts-api", "scope": "accounts.read"}]
        self.assertEqual(obo.check_chain(good), [])
        self.assertEqual(obo.check_chain([]), [])
        bad = [good[0], {**good[1], "sub_hash": "attacker", "scope": "accounts.read accounts.write", "aud": "x"}]
        self.assertEqual(len(obo.check_chain(bad)), 3)
        self.assertEqual(len(obo.check_chain([good[0], {**good[1], "aud": ["accounts-api", "payments-agent"]}])), 1)

    def test_delegate_narrows_and_audits(self):
        seen, new = {}, hop_token()
        out, rows = run_delegate(new, ["accounts.read", "accounts.write"], seen=seen)  # write not held: dropped
        self.assertEqual(out, new)
        self.assertEqual(seen["body"]["scope"], ["accounts.read aud-accounts-agent"])
        self.assertEqual(seen["body"]["audience"], ["accounts-agent"])
        self.assertEqual(rows, [{"event": "obo_hop", "trace_id": "tr-1",
                                 **obo.hop_record("advisor->accounts-agent", new)}])
        self.assertNotIn("u-123", json.dumps(rows))

    def test_delegate_refusals(self):
        def refusal(fn) -> str:
            with self.assertRaises(PermissionError) as ctx:
                fn()
            return str(ctx.exception)

        self.assertIn("scope widened", refusal(lambda: run_delegate(hop_token(scope="accounts.read accounts.write"))))
        self.assertIn("scope widened", refusal(lambda: run_delegate(hop_token(scope="accounts.read offline_access"))))
        asked: dict = {}
        self.assertIn("holds none", refusal(lambda: run_delegate(hop_token(), ["accounts.write"], seen=asked)))
        self.assertEqual(asked, {})  # refused before the IdP was called
        self.assertIn("subject changed", refusal(lambda: run_delegate(hop_token(sub="u-999"))))
        self.assertIn("subject changed", refusal(lambda: run_delegate(hop_token(sub=None))))
        self.assertNotIn("u-999", refusal(lambda: run_delegate(hop_token(sub="u-999"))))
        self.assertIn("only", refusal(lambda: run_delegate(hop_token(aud="accounts-api"))))
        self.assertIn("only", refusal(lambda: run_delegate(hop_token(aud=["accounts-agent", "accounts-api"]))))

    def test_delegate_audit_failure_fails_closed(self):
        def broken(_record):
            raise OSError("audit table unavailable")
        with self.assertRaises(obo.AuditError):
            run_delegate(hop_token(), audit=broken)


class TestAuthorizer(Quiet):
    def test_cases(self):
        for name, (tok, allowed, reason) in CASES.items():
            out = authz.authorize(bearer(tok), WRITE_POLICY, JWKS, NOW)
            self.assertIs(out["isAuthorized"], allowed, name)
            self.assertEqual(out["context"].get("reason") or "ok", reason, name)
        ok = authz.authorize(bearer(token()), WRITE_POLICY, JWKS, NOW)["context"]
        self.assertEqual((ok["username"], ok["user_sub"], ok["effective_scopes"]), ("user-a", "u-123", "accounts.write"))
        self.assertEqual(authz.authorize({"headers": {}}, WRITE_POLICY, JWKS, NOW)["context"]["reason"],
                         "missing_bearer")

    def test_fails_closed_on_missing_or_wrong_claims(self):
        for name, (tok, reason) in FAIL_CLOSED.items():
            out = authz.authorize(bearer(tok), WRITE_POLICY, JWKS, NOW)
            self.assertFalse(out["isAuthorized"], name)
            self.assertEqual(out["context"]["reason"], reason, name)
        for name, tok in ALSO_ALLOWED.items():
            self.assertTrue(authz.authorize(bearer(tok), WRITE_POLICY, JWKS, NOW)["isAuthorized"], name)
        self.assertTrue(authz.authorize(bearer(token(exp=NOW - 5, iat=NOW - 300)), WRITE_POLICY, JWKS, NOW)
                        ["isAuthorized"])  # inside the 30 s skew
        with self.assertRaises(ValueError):
            authz.Policy(issuer="", resource=RES, required_scope="s", actor_ceiling={"a": ["s"]})

    def test_malleated_signature_refused(self):
        """s + n verifies under plain modular arithmetic; RFC 8017 requires s < n, so it must be refused."""
        n = KEY.public_key().public_numbers().n
        size = (n.bit_length() + 7) // 8
        for i in range(200):  # find a signature where s + n still fits in the modulus length
            tok = token(jti=f"j{i}")
            h, p, s64 = tok.split(".")
            s = int.from_bytes(base64.urlsafe_b64decode(s64 + "=" * (-len(s64) % 4)), "big")
            if s + n < 256 ** size:
                break
        else:
            self.skipTest("no signature with room for s + n")
        self.assertTrue(authz.authorize(bearer(tok), WRITE_POLICY, JWKS, NOW)["isAuthorized"])
        forged = f"{h}.{p}." + base64.urlsafe_b64encode((s + n).to_bytes(size, "big")).rstrip(b"=").decode()
        out = authz.authorize(bearer(forged), WRITE_POLICY, JWKS, NOW)
        self.assertEqual((out["isAuthorized"], out["context"]["reason"]), (False, "bad_signature"))

    def test_trace_id_minted_server_side(self):
        caller = "0123456789abcdef0123456789abcdef"
        ctx = authz.authorize(bearer(token(), **{"X-Trace-Id": caller}), WRITE_POLICY, JWKS, NOW)["context"]
        self.assertRegex(ctx["trace_id"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(ctx["trace_id"], caller)
        self.assertEqual(ctx["caller_trace_id"], caller)
        for bad in ["t1", "x" * 65, 'abcd1234"}\n{"forged":true', "../../etc/passwd", "a b c d e f g h"]:
            ctx = authz.authorize(bearer(token(), **{"X-Trace-Id": bad}), WRITE_POLICY, JWKS, NOW)["context"]
            self.assertNotIn("caller_trace_id", ctx)
            self.assertNotIn(bad, json.dumps(ctx))

    def test_logs_hash_not_subject(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = authz.authorize(bearer(token()), WRITE_POLICY, JWKS, NOW)
        line = json.loads(buf.getvalue())
        self.assertEqual(out["context"]["user_sub"], "u-123")  # the backend still gets the customer
        self.assertEqual(line["sub_hash"], authz.subject_hash("u-123"))
        self.assertNotIn("u-123", buf.getvalue())
        self.assertNotIn("user-a", buf.getvalue())

    def test_audit_failure_fails_closed(self):
        rows: list[dict] = []
        self.assertTrue(authz.authorize(bearer(token()), WRITE_POLICY, JWKS, NOW, audit=rows.append)["isAuthorized"])
        self.assertEqual(rows[0]["result"], "allow")

        def broken(_record):
            raise OSError("table unavailable")
        before = authz.STATS["audit_write_failures"]
        allow = authz.authorize(bearer(token()), WRITE_POLICY, JWKS, NOW, audit=broken)
        deny = authz.authorize(bearer(token(key=OTHER_KEY)), WRITE_POLICY, JWKS, NOW, audit=broken)
        self.assertEqual((allow["isAuthorized"], allow["context"]["reason"]), (False, "audit_unavailable"))
        self.assertEqual((deny["isAuthorized"], deny["context"]["reason"]), (False, "bad_signature"))
        self.assertEqual(authz.STATS["audit_write_failures"], before + 2)


ENV = {"EXPECTED_ISS": ISS, "RESOURCE_NAME": RES, "REQUIRED_SCOPE": "accounts.write", "AUDIT_TABLE": "audit",
       "MAX_SCOPE_FOR_ACTOR": json.dumps(WRITE_POLICY.actor_ceiling), "ALLOWED_SUBJECT_TYPES": '["human"]',
       "MAX_TOKEN_LIFETIME_SECONDS": "780", "SUBJECT_HASH_KEY": "test-key",
       "OIDC_DISCOVERY_URL": "https://idp.example.com/realms/demo/.well-known/openid-configuration"}


class TestHandler(Quiet):
    """handler() wiring with a fake boto3 and a fake IdP (urlopen patched); nothing leaves the process."""

    def setUp(self):
        super().setUp()
        self.calls, self.fail = [], False

        def put_item(**kw):
            if self.fail:
                raise RuntimeError("ProvisionedThroughputExceededException")
            self.calls.append(kw)
        fake = types.ModuleType("boto3")
        fake.client = lambda *_a, **_k: types.SimpleNamespace(put_item=put_item)  # type: ignore[attr-defined]
        self._mods = mock.patch.dict(sys.modules, {"boto3": fake})
        self._mods.start()
        self._env = mock.patch.dict(os.environ, ENV)
        self._env.start()
        authz._ddb_audit.cache_clear()
        self.fetches = 0

    def tearDown(self):
        self._env.stop()
        self._mods.stop()
        authz._ddb_audit.cache_clear()
        authz._JWKS.update(keys=None, ts=0.0)
        super().tearDown()

    def fake_idp(self, keys, issuer=ISS):
        def urlopen(url, timeout):
            self.fetches += 1
            if url.endswith("openid-configuration"):
                return Resp(json.dumps({"issuer": issuer, "jwks_uri": "https://idp.example.com/certs"}).encode())
            return Resp(json.dumps({"keys": keys}).encode())
        return mock.patch.object(authz.urllib.request, "urlopen", urlopen)

    def test_audit_row_and_failure(self):
        authz._JWKS.update(keys=JWKS, ts=time.time())
        self.assertTrue(authz.handler(bearer(token()), None)["isAuthorized"])
        item = self.calls[0]["Item"]
        self.assertEqual(self.calls[0]["TableName"], "audit")
        self.assertEqual(item["result"], {"S": "allow"})
        self.assertEqual(item["sub_hash"]["S"], authz.subject_hash("u-123"))
        self.assertNotIn("u-123", json.dumps(self.calls))
        self.fail = True
        self.assertEqual(authz.handler(bearer(token()), None)["context"]["reason"], "audit_unavailable")

    def test_missing_hash_key_refused(self):
        with mock.patch.dict(os.environ, {"SUBJECT_HASH_KEY": ""}):
            with self.assertRaises(RuntimeError):
                authz.handler(bearer(token()), None)

    def test_rotated_key_refetched_after_floor(self):
        new_key_token = token(kid="k2")
        both = JWKS + [_jwk(KEY, "k2")]
        authz._JWKS.update(keys=JWKS, ts=time.time() - authz.JWKS_REFRESH_FLOOR_S - 5)
        with self.fake_idp(both):
            self.assertTrue(authz.handler(bearer(new_key_token), None)["isAuthorized"])
        self.assertEqual(self.fetches, 2)  # discovery + jwks

    def test_http_discovery_or_jwks_uri_refused(self):
        with mock.patch.dict(os.environ, {"OIDC_DISCOVERY_URL": "http://idp.example.com/.well-known/openid-configuration"}):
            with self.fake_idp(JWKS), self.assertRaises(ValueError):
                authz._jwks(force=True)
        self.assertEqual(self.fetches, 0)  # refused before any request
        def urlopen(url, timeout):
            self.fetches += 1
            return Resp(json.dumps({"issuer": ISS, "jwks_uri": "http://evil.example.com/certs"}).encode())
        with mock.patch.object(authz.urllib.request, "urlopen", urlopen), self.assertRaises(ValueError):
            authz._jwks(force=True)
        self.assertEqual(self.fetches, 1)  # discovery only; the http jwks_uri is never fetched

    def test_weak_key_refused(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        header, claims, reason = authz.verify_rs256(token(key=weak, kid="w1"), [_jwk(weak, "w1")])
        self.assertEqual((header, claims, reason), (None, None, "weak_key"))
        self.assertIsNotNone(authz.verify_rs256(token(), JWKS)[1])  # a 2048-bit key still verifies
        for e in (3, 65538):  # small exponent, even exponent; checked before the signature
            jwk = {**_jwk(KEY, "k1"), "e": _b64u(e)}
            self.assertEqual(authz.verify_rs256(token(), [jwk])[2], "weak_key")

    def test_unknown_kid_inside_floor_not_refetched(self):
        authz._JWKS.update(keys=JWKS, ts=time.time() - 10)
        with self.fake_idp(JWKS):
            out = authz.handler(bearer(token(kid="random")), None)
        self.assertEqual((out["isAuthorized"], out["context"]["reason"]), (False, "unknown_kid"))
        self.assertEqual(self.fetches, 0)

    def test_discovery_issuer_mismatch_raises(self):
        with self.fake_idp(JWKS, issuer="https://other.example.com"):
            with self.assertRaises(ValueError):
                authz.handler(bearer(token()), None)


if __name__ == "__main__":
    unittest.main()

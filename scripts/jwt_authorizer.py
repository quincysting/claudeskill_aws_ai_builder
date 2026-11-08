"""API Gateway (HTTP API, simple responses) Lambda authorizer for an OBO resource server. Stdlib only.

Generalized from the lab's accounts authorizer. Fails closed: a MISSING claim is a deny, never a skipped
check (the lab version skipped exp/lifetime when exp was absent). Checks, in this order:

   1. alg pinned to RS256: the header's alg is checked, never used to choose the verifier ('none', HS256 ->
      unsupported_alg); the JWKS key must be an RSA signing key
   2. RS256 signature against the key named by kid              -> verify_rs256(); claims are parsed only after.
      The signature integer must be below the modulus (RFC 8017 RSAVP1), so s + n is not a second valid
      signature for the same token
   3. exp present and not expired (CLOCK_SKEW_SECONDS leeway)
   4. iat present and not in the future; nbf (if present) not in the future; 0 < exp - iat <= max lifetime
   5. iss == EXPECTED_ISS
   6. aud == RESOURCE_NAME and nothing else
   7. an access token, not an ID token (same IdP, same keys): needs a positive marker (header typ at+jwt,
      token_use == access, or typ in ALLOWED_TOKEN_TYPES) and no ID marker (typ ID, token_use id)
   8. actor (client_id, else azp) on the allowlist = the keys of MAX_SCOPE_FOR_ACTOR
   9. REQUIRED_SCOPE in the token's scope
  10. policy claims: sub present; subject_type in ALLOWED_SUBJECT_TYPES (when set)
  11. per-actor ceiling: REQUIRED_SCOPE among the granted resource roles, and all granted roles within
      MAX_SCOPE_FOR_ACTOR[actor]. The roles are read from Keycloak's resource_access[RESOURCE_NAME].roles;
      other IdPs (Cognito, Entra, Okta) put roles elsewhere, so adapt step 11 before using it with them
      (without the claim every token is denied missing_role_grant)

Keys: the JWKS is cached for JWKS_TTL_S. A token whose kid is not in the cache triggers one refetch, at most
every JWKS_REFRESH_FLOOR_S, so a key rotation at the IdP doesn't deny everyone for an hour and random kids
can't make the function hammer the IdP. The discovery document's issuer must equal EXPECTED_ISS.

decide() runs steps 3-11 and trusts its input: call it ONLY with claims returned by verify_rs256 (or by
another verifier). The customer is taken from the token's sub / preferred_username, never from a request
parameter; those raw values go to the backend in the authorizer context only. Logs and audit rows carry
sub_hash (HMAC-SHA256 keyed by SUBJECT_HASH_KEY), never the raw sub or username.

trace_id is minted here and is the record's key. A caller's X-Trace-Id is kept only as caller_trace_id,
and only if it matches TRACE_ID_RE.

Audit (documented choice): when the audit write fails, an allow becomes a deny (reason audit_unavailable),
so nothing proceeds unaudited; a deny stays a deny. Every failure is logged as event=audit_write_failed
(put a metric filter + alarm on it) and counted in STATS.

Run one function per required scope (read gate, write gate) with different env config.
Env: OIDC_DISCOVERY_URL, EXPECTED_ISS, RESOURCE_NAME, REQUIRED_SCOPE, AUDIT_TABLE,
     MAX_SCOPE_FOR_ACTOR='{"payments-agent":["accounts.write"]}', ALLOWED_SUBJECT_TYPES='["human"]',
     ALLOWED_TOKEN_TYPES='["Bearer"]', MAX_TOKEN_LIFETIME_SECONDS=300, CLOCK_SKEW_SECONDS=30,
     SUBJECT_HASH_KEY (required by handler(); read it from a secret store)
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable

_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
_JWKS = {"keys": None, "ts": 0.0}
JWKS_TTL_S = 3600
JWKS_REFRESH_FLOOR_S = 300
TRACE_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
STATS = {"audit_write_failures": 0}  # per container; the audit_write_failed log line is the durable signal


@dataclass(frozen=True)
class Policy:
    issuer: str
    resource: str
    required_scope: str
    actor_ceiling: dict[str, list[str]]
    subject_types: list[str] = field(default_factory=list)
    token_types: list[str] = field(default_factory=lambda: ["Bearer"])
    max_lifetime: int = 300
    clock_skew: int = 30

    def __post_init__(self):
        if not (self.issuer and self.resource and self.required_scope and self.actor_ceiling and self.max_lifetime > 0):
            raise ValueError("policy needs issuer, resource, required_scope, actor_ceiling and max_lifetime > 0")

    @classmethod
    def from_env(cls, env=os.environ) -> "Policy":
        return cls(issuer=env["EXPECTED_ISS"], resource=env["RESOURCE_NAME"], required_scope=env["REQUIRED_SCOPE"],
                   actor_ceiling=json.loads(env.get("MAX_SCOPE_FOR_ACTOR", "{}")),
                   subject_types=json.loads(env.get("ALLOWED_SUBJECT_TYPES", "[]")),
                   token_types=json.loads(env.get("ALLOWED_TOKEN_TYPES", '["Bearer"]')),
                   max_lifetime=int(env.get("MAX_TOKEN_LIFETIME_SECONDS", "300")),
                   clock_skew=int(env.get("CLOCK_SKEW_SECONDS", "30")))


def _b64(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _num(value) -> int | float | None:
    """A JWT NumericDate, or None when missing or not a number (bools excluded)."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def scopes_of(claims: dict) -> set[str]:
    """Scopes from `scope` (space-separated, RFC 8693/9068) or `scp` (string or list, Okta/Entra)."""
    s = claims.get("scope", claims.get("scp")) or ""
    return set(s.split() if isinstance(s, str) else s if isinstance(s, list) else [])


def subject_hash(sub: str, key: str | None = None) -> str:
    """Stable pseudonym for logs/audit, keyed by SUBJECT_HASH_KEY (read at call time). Keep the key in a
    secret store so an email-like sub can't be reversed by hashing guesses. Same function as
    obo_exchange.subject_hash, so rows join across hops."""
    key = os.environ.get("SUBJECT_HASH_KEY", "") if key is None else key
    return hmac.new(key.encode(), sub.encode(), hashlib.sha256).hexdigest()[:32]


MIN_RSA_BITS = 2048  # a short modulus from a misconfigured IdP must not be trusted


def verify_rs256(token: str, jwks: list[dict]) -> tuple[dict | None, dict | None, str | None]:
    """(header, claims, None) only if the RS256 PKCS#1 v1.5 signature verifies; else (None, None, reason)."""
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(_b64(h64))
        if header.get("alg") != "RS256":
            return None, None, "unsupported_alg"
        key = next((k for k in jwks if k.get("kid") == header.get("kid") and k.get("kty") == "RSA"
                    and k.get("use", "sig") == "sig" and k.get("alg", "RS256") == "RS256"), None)
        if not key:
            return None, None, "unknown_kid"
        n, e = int.from_bytes(_b64(key["n"]), "big"), int.from_bytes(_b64(key["e"]), "big")
        if n.bit_length() < MIN_RSA_BITS or e < 65537 or e % 2 == 0:
            return None, None, "weak_key"
        size, sig = (n.bit_length() + 7) // 8, _b64(s64)
        s = int.from_bytes(sig, "big")
        if len(sig) != size or s >= n:  # RFC 8017 RSAVP1: out-of-range s is refused, not reduced mod n
            return None, None, "bad_signature"
        em = pow(s, e, n).to_bytes(size, "big")
        digest = hashlib.sha256(f"{h64}.{p64}".encode()).digest()
        expected = b"\x00\x01" + b"\xff" * (size - len(_SHA256_DIGESTINFO) - len(digest) - 3) + b"\x00" + _SHA256_DIGESTINFO + digest
        if em != expected:
            return None, None, "bad_signature"
        claims = json.loads(_b64(p64))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, None, "malformed_token"
    return (header, claims, None) if isinstance(claims, dict) else (None, None, "malformed_token")


def _is_access_token(header: dict, claims: dict, token_types: list[str]) -> bool:
    """ID tokens verify against the same keys, so require an access-token marker and no ID-token marker."""
    if claims.get("typ") == "ID" or claims.get("token_use") == "id":
        return False
    return (str(header.get("typ", "")).lower() in ("at+jwt", "application/at+jwt")
            or claims.get("token_use") == "access" or claims.get("typ") in token_types)


def decide(header: dict, claims: dict, p: Policy, now: int) -> tuple[bool, str, dict]:
    """Steps 3-11 on claims a verifier already returned: (allowed, reason, context). No signature check here."""
    exp, iat, nbf = _num(claims.get("exp")), _num(claims.get("iat")), claims.get("nbf")
    if exp is None:
        return False, "missing_exp", {}
    if exp + p.clock_skew <= now:
        return False, "token_expired", {}
    if iat is None:
        return False, "missing_iat", {}
    if iat > now + p.clock_skew:
        return False, "token_issued_in_future", {}
    if nbf is not None and (_num(nbf) is None or nbf > now + p.clock_skew):
        return False, "token_not_yet_valid", {}
    if exp - iat > p.max_lifetime:
        return False, "token_lifetime_too_long", {"lifetime": exp - iat}
    if exp <= iat:
        return False, "invalid_lifetime", {}
    if claims.get("iss") != p.issuer:
        return False, "wrong_issuer", {}
    aud = claims.get("aud")
    if aud not in (p.resource, [p.resource]):
        return False, "wrong_audience", {"aud": aud}
    if not _is_access_token(header, claims, p.token_types):
        return False, "wrong_token_type", {"typ": claims.get("typ"), "token_use": claims.get("token_use")}
    actor = claims.get("client_id") or claims.get("azp")
    if actor not in p.actor_ceiling:
        return False, "unknown_actor", {"client_id": actor}
    scopes = scopes_of(claims)
    if p.required_scope not in scopes:
        return False, "insufficient_scope", {"scopes": sorted(scopes)}
    if not claims.get("sub"):
        return False, "missing_sub", {}
    if p.subject_types and claims.get("subject_type") not in p.subject_types:
        return False, "subject_type_not_allowed", {"subject_type": claims.get("subject_type")}
    ceiling = set(p.actor_ceiling[actor])
    granted = set(((claims.get("resource_access") or {}).get(p.resource) or {}).get("roles") or [])
    if p.required_scope not in granted:
        return False, "missing_role_grant", {"client_id": actor}
    if granted - ceiling:
        return False, "actor_over_ceiling", {"client_id": actor, "over": sorted(granted - ceiling)}
    return True, "ok", {"user_sub": claims["sub"], "username": claims.get("preferred_username") or "",
                        "client_id": actor, "subject_type": claims.get("subject_type") or "",
                        "effective_scopes": ",".join(sorted(granted & ceiling & scopes))}


def trace_ids(headers: dict) -> dict:
    """The server-minted trace_id is the record; a caller's X-Trace-Id is kept for correlation only if well-formed."""
    ids = {"trace_id": uuid.uuid4().hex}
    caller = headers.get("x-trace-id")
    if isinstance(caller, str) and TRACE_ID_RE.fullmatch(caller):
        ids["caller_trace_id"] = caller
    return ids


def _loggable(ctx: dict) -> dict:
    """Decision context safe for logs/audit: raw sub and username replaced by sub_hash."""
    out = {k: v for k, v in ctx.items() if k not in ("user_sub", "username")}
    if ctx.get("user_sub"):
        out["sub_hash"] = subject_hash(str(ctx["user_sub"]))
    return out


def authorize(event: dict, policy: Policy, jwks: list[dict], now: int | None = None,
              audit: Callable[[dict], None] | None = None) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    ids = trace_ids(headers)
    auth = headers.get("authorization", "")
    allowed, reason, ctx = False, "missing_bearer", {}
    if auth.startswith("Bearer "):
        header, claims, reason = verify_rs256(auth[7:], jwks)
        if header is not None and claims is not None:
            try:
                allowed, reason, ctx = decide(header, claims, policy, int(time.time() if now is None else now))
            except (TypeError, ValueError, AttributeError):
                allowed, reason, ctx = False, "malformed_claims", {}
    record = {"event": "authz_decision", "resource": policy.resource, "result": "allow" if allowed else "deny",
              "reason": reason, **ids, **_loggable(ctx)}
    if audit is not None:
        try:
            audit(record)
        except Exception as error:  # surfaced and counted; an allow fails closed
            STATS["audit_write_failures"] += 1
            print(json.dumps({"event": "audit_write_failed", "resource": policy.resource, **ids,
                              "count": STATS["audit_write_failures"], "detail": str(error)[:200]}))
            if allowed:
                allowed, reason = False, "audit_unavailable"
                record.update(result="deny", reason=reason)
    print(json.dumps(record, default=str))
    context = {k: str(v) for k, v in ctx.items()} if allowed else {"reason": reason}
    return {"isAuthorized": allowed, "context": {**context, **ids}}


@functools.lru_cache(maxsize=None)
def _ddb_audit(table: str) -> Callable[[dict], None]:
    """One DynamoDB row per decision (PK trace_id, SK seq). Errors propagate to authorize()."""
    import boto3  # provided by the Lambda runtime; imported lazily so this module stays stdlib-only

    ddb = boto3.client("dynamodb")

    def write(record: dict) -> None:
        now_ms = int(time.time() * 1000)
        row = {**record, "seq": f"{now_ms:013d}#{record['resource']}#{record['result']}", "ts": now_ms}
        ddb.put_item(TableName=table, Item={k: {"S": v if isinstance(v, str) else json.dumps(v, default=str)}
                                            for k, v in row.items()})
    return write


def _https(url: object) -> str:
    """Keys fetched over http (or file://) could be swapped in transit, which forges every token."""
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise ValueError("OIDC discovery and JWKS URLs must use https")
    return url


def _jwks(force: bool = False) -> list[dict]:
    """Cached signing keys. force=True refetches, but only once the cache is JWKS_REFRESH_FLOOR_S old."""
    age = time.time() - _JWKS["ts"]
    if _JWKS["keys"] and age < JWKS_TTL_S and not (force and age >= JWKS_REFRESH_FLOOR_S):
        return _JWKS["keys"]
    disc = json.loads(urllib.request.urlopen(_https(os.environ["OIDC_DISCOVERY_URL"]), timeout=10).read())
    if disc.get("issuer") != os.environ["EXPECTED_ISS"]:  # OIDC Discovery: issuer must match exactly
        raise ValueError("discovery issuer does not match EXPECTED_ISS")
    _JWKS.update(keys=json.loads(urllib.request.urlopen(_https(disc.get("jwks_uri")), timeout=10).read())["keys"], ts=time.time())
    return _JWKS["keys"]


def _token_kid(event: dict) -> str | None:
    """The kid in the bearer token's header, read unverified only to decide whether to refetch keys."""
    auth = {k.lower(): v for k, v in (event.get("headers") or {}).items()}.get("authorization", "")
    try:
        return json.loads(_b64(auth[7:].split(".")[0])).get("kid")
    except (ValueError, AttributeError, TypeError):
        return None


def handler(event, context):
    if not os.environ.get("SUBJECT_HASH_KEY"):
        raise RuntimeError("SUBJECT_HASH_KEY is required: logs and audit rows carry an HMAC of sub")
    keys = _jwks()
    if _token_kid(event) not in {k.get("kid") for k in keys}:  # the IdP may have rotated its keys
        keys = _jwks(force=True)
    return authorize(event, Policy.from_env(), keys, audit=_ddb_audit(os.environ["AUDIT_TABLE"]))

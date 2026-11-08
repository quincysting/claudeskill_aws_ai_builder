"""On-Behalf-Of (OBO) token exchange helpers, RFC 8693. Framework-free (stdlib only).

Works with any IdP that implements token exchange (Keycloak "Standard Token Exchange", Okta,
Entra OBO, Cognito + custom). Each agent hop:
    inbound user token (aud = me)  --exchange-->  new token (same sub, aud = next hop, narrower scope)

delegate() does one hop and refuses (PermissionError) unless the new token
  - keeps the user's sub,
  - is addressed to the next hop and nothing else,
  - carries no scope beyond (requested ∩ inbound scope), apart from identity scopes
    (openid/email/profile) the inbound token already had.
It writes the hop's audit row BEFORE returning the token; if that write fails it raises AuditError and the
token is never handed back, so the hop can't proceed unaudited (fail closed).

Trust model: this module reads claims WITHOUT verifying signatures (decode_claims). That is safe only
because (a) the inbound token was already verified by the inbound JWT authorizer in front of this code
(AgentCore Runtime customJWTAuthorizer: signature, iss, aud, client, scope; every endpoint and version
must have one), and (b) the new token comes straight from the IdP's token endpoint over TLS.
Resource servers must verify for themselves (jwt_authorizer.py).

Audit rows and logs carry sub_hash (HMAC-SHA256 keyed by SUBJECT_HASH_KEY), never the raw sub, username
or email.

Keycloak specifics baked in as defaults:
- The calling agent is a CONFIDENTIAL client with token exchange enabled; it authenticates with its
  own client_id/secret (keep the secret in Secrets Manager, not in env/plaintext).
- The audience is set by requesting an optional client scope "aud-<next_hop>" (an audience mapper)
  in addition to `audience=<next_hop>`. A client that doesn't have that scope assigned can't mint
  that audience -> "bounded reach".
- Roles in the new token are capped by the client's own scope mappings -> "agent ceiling".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
from typing import Callable, Iterable

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
IDENTITY_SCOPES = ("openid", "email", "profile")  # may pass through a hop if already held; never added by it


class AuditError(RuntimeError):
    """The hop's audit row could not be written; the hop must not proceed."""


def exchange_form(subject_token: str, client_id: str, client_secret: str, next_hop: str,
                  scopes: Iterable[str], audience_scope_prefix: str | None = "aud-") -> dict:
    """The x-www-form-urlencoded body for an RFC 8693 exchange to `next_hop`."""
    scope = list(scopes) + ([audience_scope_prefix + next_hop] if audience_scope_prefix else [])
    return {
        "grant_type": TOKEN_EXCHANGE,
        "client_id": client_id,
        "client_secret": client_secret,
        "subject_token": subject_token,
        "subject_token_type": ACCESS_TOKEN,
        "requested_token_type": ACCESS_TOKEN,
        "audience": next_hop,
        "scope": " ".join(scope),
    }


def exchange_token(token_endpoint: str, form: dict, timeout: int = 20, opener=urllib.request.urlopen) -> str:
    """POST the exchange and return the new access token. HTTPError (400 invalid_scope /
    access_denied) means the IdP refused: wrong audience for this client, or scope over its ceiling.
    The form carries the client secret and the user's token, so plain http is refused."""
    if not token_endpoint.lower().startswith("https://"):
        raise ValueError("token endpoint must use https")
    request = urllib.request.Request(token_endpoint, data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with opener(request, timeout=timeout) as response:
        body = json.loads(response.read())
    access = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access, str) or not access:
        raise PermissionError("token endpoint returned no access_token")  # treat as a refusal
    return access


def bearer_from_headers(headers: dict | None) -> str:
    """The caller's bearer token (header names are case-insensitive). On AgentCore Runtime the
    Authorization header only reaches the code if it's in requestHeaderAllowlist."""
    for key, value in (headers or {}).items():
        if key.lower() == "authorization" and value.lower().startswith("bearer "):
            return value.split(" ", 1)[1]
    raise PermissionError("no inbound bearer token")


def decode_claims(token: str) -> dict:
    """Claims WITHOUT signature verification. Only for a token already verified by the authorizer in front
    of this code, or one just received from the IdP over TLS (see the module docstring). Never use it to
    authorize at a resource server."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def scopes_of(claims: dict) -> set[str]:
    """Scopes from `scope` (space-separated, RFC 8693/9068) or `scp` (string or list, Okta/Entra)."""
    s = claims.get("scope", claims.get("scp")) or ""
    return set(s.split() if isinstance(s, str) else s if isinstance(s, list) else [])


def subject_hash(sub: str, key: str | None = None) -> str:
    """Stable pseudonym for logs/audit, keyed by SUBJECT_HASH_KEY (read at call time); the same function as
    jwt_authorizer.subject_hash, so rows join across hops. Keep the key in a secret store so an email-like
    sub can't be reversed by hashing guesses."""
    key = os.environ.get("SUBJECT_HASH_KEY", "") if key is None else key
    return hmac.new(key.encode(), sub.encode(), hashlib.sha256).hexdigest()[:32]


def check_hop(prev: dict, new: dict, next_hop: str, allowed: Iterable[str],
              keep: Iterable[str] = IDENTITY_SCOPES) -> list[str]:
    """Delegation invariants for one hop, on claims: same sub, aud == next_hop only, scope within
    `allowed` (plus `keep` scopes prev already held). Returns violations (no raw subjects in them)."""
    problems = []
    if not prev.get("sub") or new.get("sub") != prev.get("sub"):
        problems.append("subject changed or missing")
    if new.get("aud") not in (next_hop, [next_hop]):
        problems.append(f"aud {new.get('aud')!r} is not {next_hop!r} only")
    widened = scopes_of(new) - (set(allowed) | (scopes_of(prev) & set(keep)))
    if widened:
        problems.append(f"scope widened: {sorted(widened)}")
    return problems


def hop_record(name: str, token: str, status: int | None = None) -> dict:
    """One audit/chain entry: selected claims, sub as sub_hash; never the raw token, sub, username or email."""
    c = decode_claims(token)
    rec = {"hop": name, "sub_hash": subject_hash(str(c["sub"])) if c.get("sub") else None,
           "aud": c.get("aud"), "scope": c.get("scope"), "subject_type": c.get("subject_type"),
           "client_id": c.get("client_id") or c.get("azp")}
    if status is not None:
        rec["downstream_status"] = status
    return rec


def delegate(subject_token: str, next_hop: str, requested_scopes: Iterable[str] | str, *, token_endpoint: str,
             client_id: str, client_secret: str, audit: Callable[[dict], None], trace_id: str,
             keep: Iterable[str] = IDENTITY_SCOPES, audience_scope_prefix: str | None = "aud-",
             opener=urllib.request.urlopen) -> str:
    """One OBO hop. Asks only for requested ∩ inbound scope, checks the IdP's answer with check_hop(),
    writes the audit row, then returns the new token.
    Raises PermissionError (nothing to delegate, or an invariant broken) or AuditError (write failed)."""
    prev = decode_claims(subject_token)
    requested = set(requested_scopes.split() if isinstance(requested_scopes, str) else requested_scopes)
    allowed = requested & scopes_of(prev)
    if not allowed:
        raise PermissionError(f"refused: inbound token holds none of {sorted(requested)}")
    form = exchange_form(subject_token, client_id, client_secret, next_hop, sorted(allowed), audience_scope_prefix)
    new = exchange_token(token_endpoint, form, opener=opener)
    problems = check_hop(prev, decode_claims(new), next_hop, allowed, keep)
    if problems:
        raise PermissionError("refused: " + "; ".join(problems))
    record = {"event": "obo_hop", "trace_id": trace_id, **hop_record(f"{client_id}->{next_hop}", new)}
    try:
        audit(record)
    except Exception as error:
        raise AuditError(f"audit write failed for {record['hop']}: {error}") from error
    return new


def check_chain(chain: list[dict]) -> list[str]:
    """Post-hoc check of hop_record()s (hop names like "advisor->accounts-agent"): same sub_hash on every
    hop, aud == the hop's target only, scope never wider than the previous hop's. Returns violations."""
    def as_claims(r: dict) -> dict:
        return {"sub": r.get("sub_hash"), "aud": r.get("aud"), "scope": r.get("scope")}

    problems = []
    for prev, cur in zip(chain[:1] + chain, chain):
        target = cur["hop"].split("->")[-1]
        problems += [f"{cur['hop']}: {p}" for p in
                     check_hop(as_claims(prev), as_claims(cur), target, scopes_of(prev), keep=())]
    return problems

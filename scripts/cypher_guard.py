"""Graph reads for agents: named traversals first, a guarded free-form Cypher tool as a fallback.

Controls, strongest first:
1. A read-only database identity. This is the real boundary; everything below is in addition to it.
   - Neo4j Enterprise: connect as a user whose roles only grant read (MATCH/TRAVERSE/READ), revoke
     EXECUTE PROCEDURE and EXECUTE FUNCTIONS from PUBLIC, and set db.transaction.timeout server-side.
     Community edition has no RBAC: don't expose the free-form tool there.
   - Amazon Neptune: the caller's IAM policy allows neptune-db:ReadDataViaQuery only, not
     WriteDataViaQuery/DeleteDataViaQuery, so the database itself refuses writes.
2. Named, parameterised traversals (TRAVERSALS): the model picks a name and fills values; it never writes
   Cypher. Prefer adding a traversal over enabling the free-form tool.
3. run_read: a READ transaction with a timeout, a LIMIT inside the query, and at most MAX_ROWS fetched.
   A read transaction blocks writes but NOT procedure or plugin function calls, hence 1 and 4.
4. check(): a keyword scan of free-form text, defence in depth only. It over-blocks (a string literal with
   'set' in it, a URL with //) and under-blocks whatever the list forgot. Pass values as $parameters.
   LIMIT caps rows, not work: an eager aggregate can still burn the whole timeout.

Refusals raise QueryRejected(reason, detail). FastMCP turns a tool exception into a result with
isError=true, so the client sees a tool error, not a success string to pattern-match.

CLI: python cypher_guard.py "MATCH (n) RETURN n LIMIT 5"
     exit 0 allowed, 1 rejected (reason printed), 2 usage.
Standard library only; run_read imports the neo4j driver when it is called.
Tests: test_cypher_guard.py (offline, fake driver).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from itertools import islice
from typing import Any

MAX_ROWS = 50
TIMEOUT_S = 5.0
READ_PREFIXES = ("MATCH", "OPTIONAL MATCH", "WITH", "UNWIND")
FORBIDDEN = ("CALL", "APOC", "DBMS", "DB", "LOAD CSV", "CREATE", "INSERT", "MERGE", "DELETE", "DETACH",
             "SET", "REMOVE", "DROP", "FOREACH", "UNION", "USE")  # UNION would escape the single trailing LIMIT
# Built-in function namespaces that only compute values. Any other dotted call (a plugin or user-defined
# function, such as gds.* or genai.*) runs without CALL, so it is refused.
SAFE_NAMESPACES = {"POINT", "VECTOR.SIMILARITY", "DATE", "DATETIME", "LOCALDATETIME", "LOCALTIME", "TIME",
                   "DURATION"}
_PART = r"(?:`[^`]*`|[^\W\d]\w*)"
_NAMESPACED_CALL = re.compile(rf"((?:{_PART} ?\. ?)+){_PART} ?\(")
_PLAIN_SPACE = " \t\r\n"

TRAVERSALS: dict[str, tuple[str, set[str]]] = {  # name -> (reviewed Cypher, required parameters)
    "metrics_for_entity": (
        "MATCH (e:Entity {id: $entity_id})-[:INSTANCE_OF]->(c:OntologyClass)<-[:COMPUTED_OVER]-(m:GovernedMetric) "
        f"RETURN c.name AS class, m.name AS metric LIMIT {MAX_ROWS}", {"entity_id"}),
    "subclasses_of": (
        "MATCH (c:OntologyClass)-[:SUBCLASS_OF]->(:OntologyClass {uri: $parent_uri}) "
        f"RETURN c.uri AS uri, c.name AS name LIMIT {MAX_ROWS}", {"parent_uri"}),
}


class QueryRejected(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"rejected [{reason}]: {detail}")
        self.reason, self.detail = reason, detail


def traversal(name: str, params: dict) -> tuple[str, dict]:
    """The reviewed Cypher for `name`, with params checked against what it declares."""
    if name not in TRAVERSALS:
        raise QueryRejected("unknown_traversal", f"'{name}' is not one of {sorted(TRAVERSALS)}")
    cypher, required = TRAVERSALS[name]
    if set(params) != required:
        raise QueryRejected("bad_parameters", f"'{name}' takes exactly {sorted(required)}")
    return cypher, dict(params)


def check(cypher: str) -> None:
    """Raise QueryRejected unless the text is one read query ending in LIMIT n (n <= MAX_ROWS)."""
    query = (cypher or "").strip().rstrip(";").strip()
    # The scan below splits words on Python whitespace. A separator the database accepts but Python
    # doesn't split on (U+180E, zero-width characters) would hide "LOAD CSV", so refuse them all.
    odd = next((c for c in query if c not in _PLAIN_SPACE and unicodedata.category(c)[0] in "ZC"), None)
    if odd is not None:
        raise QueryRejected("bad_character", f"character U+{ord(odd):04X} is not allowed")
    normalized = " ".join(query.upper().split())
    if ";" in query or not normalized.startswith(READ_PREFIXES) or " RETURN " not in f" {normalized} ":
        raise QueryRejected("not_single_read", "only a single read query (MATCH ... RETURN ... LIMIT n) is allowed")
    if "//" in query or "/*" in query:
        raise QueryRejected("comment", "comments are not allowed")
    for word in FORBIDDEN:
        # Whole word, so `created_at` or `OFFSET` isn't flagged; DB/DBMS only as a namespace (db.x, `db`.x).
        tail = r"`?\s*\." if word in ("DB", "DBMS") else r"(?![A-Z0-9_])"
        if re.search(r"(?<![A-Z0-9_])" + re.escape(word) + tail, normalized):
            raise QueryRejected("forbidden_clause", f"'{word}' is not allowed in a read tool")
    for call in _NAMESPACED_CALL.finditer(normalized):
        namespace = re.sub(r"[` ]", "", call.group(1)).rstrip(".")
        if namespace not in SAFE_NAMESPACES:
            raise QueryRejected("forbidden_function", f"functions in '{namespace.lower()}' are not allowed")
    limit = re.search(r"(?<![A-Z0-9_])LIMIT (\d+)$", normalized)
    if not limit:
        raise QueryRejected("missing_limit", f"end the query with LIMIT n, n <= {MAX_ROWS}")
    if int(limit.group(1)) > MAX_ROWS:
        raise QueryRejected("limit_too_high", f"LIMIT {limit.group(1)} > {MAX_ROWS}")


def run_read(driver: Any, cypher: str, params: dict | None = None, database: str | None = None) -> list[dict]:
    """Run in a READ transaction with a server-side timeout; fetch at most MAX_ROWS records."""
    import neo4j  # lazy, so the tests run without the driver

    @neo4j.unit_of_work(timeout=TIMEOUT_S)
    def work(tx: Any) -> list[dict]:
        return [r.data() for r in islice(tx.run(cypher, params or {}), MAX_ROWS)]

    with driver.session(database=database, default_access_mode=neo4j.READ_ACCESS) as session:
        return session.execute_read(work)


def register(mcp: Any, driver: Any, free_form: bool = False) -> None:
    """Add the graph tools to a FastMCP server. free_form=True only for staff-facing agents."""

    @mcp.tool()
    def graph_traversal(name: str, params: dict[str, str]) -> str:
        """Run a reviewed graph traversal. metrics_for_entity(entity_id): governed metrics for an entity.
        subclasses_of(parent_uri): ontology classes directly under a class."""
        return json.dumps(run_read(driver, *traversal(name, params)), default=str)

    if free_form:
        @mcp.tool()
        def execute_read_cypher(cypher_query: str, params: dict[str, str | int | float | bool] | None = None) -> str:
            """One read-only Cypher query: MATCH ... RETURN ... LIMIT n (n <= 50). Put values in params
            and refer to them as $name in the query, never inline them."""
            check(cypher_query)
            return json.dumps(run_read(driver, cypher_query, params), default=str)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].strip():
        print('usage: python cypher_guard.py "<cypher query>"', file=sys.stderr)
        return 2
    try:
        check(argv[0])
    except QueryRejected as err:
        print(err)
        return 1
    print("allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Offline tests for cypher_guard.py: the keyword guard, named traversals, the read runner and the tools.

No database, no network: a fake `neo4j` module and driver, and a fake MCP server that records tools.
Run: python -B -m unittest test_cypher_guard
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cypher_guard as cg  # noqa: E402

READS = [
    "MATCH (c:OntologyClass) RETURN c.name LIMIT 10",
    "MATCH (n) WHERE n.created_at > 0 RETURN n SKIP 1 LIMIT 5",  # created_at, SKIP aren't keyword hits
    "OPTIONAL MATCH (e:Entity {id: $id})-[:INSTANCE_OF]->(c) RETURN c.name AS class ORDER BY class LIMIT 50",
    "MATCH (a), (b) RETURN point.distance(a.loc, b.loc) AS d LIMIT 5",        # built-in namespaces pass
    "MATCH (n) RETURN vector.similarity.cosine(n.v, $q) AS s ORDER BY s DESC LIMIT 5",
    "MATCH (n) WHERE n.at > date.truncate('month', date()) RETURN count(n) AS c LIMIT 1",
    "MATCH (n) WHERE n.x = (1 + 2) RETURN n.name, toLower(n.title) LIMIT 3",  # property then '(' expression
    "MATCH (n)\n\tRETURN n\r\nLIMIT 2",
]
REJECTS = {
    "CALL dbms.components() YIELD name RETURN name LIMIT 1": "not_single_read",
    "MATCH (n) WITH n CALL apoc.help('x') YIELD name RETURN name LIMIT 1": "forbidden_clause",
    "match (n) with n call { match (m) return m } return n limit 1": "forbidden_clause",  # case is ignored
    "MATCH (n) RETURN apoc.convert.toJson(n) LIMIT 1": "forbidden_clause",
    "MATCH (n) RETURN db.nameFromElementId('x') LIMIT 1": "forbidden_clause",
    "MATCH (n) RETURN `db` . `x`() LIMIT 1": "forbidden_clause",
    "MATCH (n) WITH n LOAD CSV FROM 'https:x' AS r RETURN r LIMIT 1": "forbidden_clause",
    "MATCH (n) WITH n LOAD\tCSV FROM 'https:x' AS r RETURN r LIMIT 1": "forbidden_clause",
    "MATCH (n) SET n.x = 1 RETURN n LIMIT 1": "forbidden_clause",
    "MATCH (n) MERGE (m:X) RETURN m LIMIT 1": "forbidden_clause",
    "MATCH (n) INSERT (:X {a: 1}) RETURN n LIMIT 1": "forbidden_clause",       # GQL synonym for CREATE
    "MATCH (n) DETACH DELETE n RETURN 1 LIMIT 1": "forbidden_clause",
    "MATCH (n) REMOVE n.x RETURN n LIMIT 1": "forbidden_clause",
    "MATCH (n) FOREACH (x IN [1] | CREATE (:Y)) RETURN n LIMIT 1": "forbidden_clause",
    "MATCH (n) RETURN n UNION MATCH (m) RETURN m LIMIT 1": "forbidden_clause",
    "MATCH (n) RETURN genai.vector.encode(n.text, 'OpenAI', {}) LIMIT 1": "forbidden_function",
    "MATCH (n) RETURN `genai`.vector.encode(n.text, 'X', {}) LIMIT 1": "forbidden_function",
    "MATCH (n) RETURN gds.util.asNode(1) LIMIT 1": "forbidden_function",
    "MATCH (n) RETURN mypoint.distance(1, 2) LIMIT 1": "forbidden_function",  # not the built-in namespace
    "MATCH (n) WITH n LOAD᠎CSV FROM 'https:x' AS r RETURN r LIMIT 1": "bad_character",
    "MATCH (n) WITH n CA​LL x RETURN n LIMIT 1": "bad_character",
    "MATCH (n) RETURN n LIMIT 1; MATCH (m) DELETE m": "not_single_read",
    "DROP CONSTRAINT x": "not_single_read",
    "EXPLAIN MATCH (n) RETURN n LIMIT 1": "not_single_read",
    "MATCH (n) RETURN n // LIMIT 1": "comment",
    "MATCH (n) RETURN n /* x */ LIMIT 1": "comment",
    "MATCH (n) RETURN n": "missing_limit",
    "MATCH (n) RETURN n LIMIT 10 SKIP 0": "missing_limit",
    "MATCH (n) RETURN n, 'LIMIT 5'": "missing_limit",
    "MATCH (n) RETURN n LIMIT $k": "missing_limit",
    "MATCH (n) RETURN n LIMIT 5000": "limit_too_high",
    "": "not_single_read",
}


def install_fake_neo4j() -> None:
    mod = types.ModuleType("neo4j")
    mod.READ_ACCESS = "READ"  # type: ignore[attr-defined]

    def unit_of_work(timeout=None, metadata=None):
        def wrap(fn):
            fn.timeout = timeout
            return fn
        return wrap
    mod.unit_of_work = unit_of_work  # type: ignore[attr-defined]
    sys.modules["neo4j"] = mod


class FakeDriver:
    """neo4j driver + session + transaction in one; no write methods, so a write can't happen."""

    def __init__(self, rows: int = 500):
        self.rows, self.pulled, self.calls = rows, 0, []

    def session(self, **kw):
        self.calls.append(("session", kw))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute_read(self, work):
        self.calls.append(("execute_read", work.timeout))
        return work(self)

    def run(self, cypher, params):  # tx.run: a lazy record stream
        self.calls.append(("tx.run", cypher, params))
        for i in range(self.rows):
            self.pulled += 1
            yield types.SimpleNamespace(data=lambda i=i: {"i": i})


class FakeMCP:
    """Records the functions registered with @mcp.tool()."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn
        return wrap


class CheckTest(unittest.TestCase):
    def test_reads_pass(self):
        for q in READS:
            with self.subTest(q=q):
                cg.check(q)

    def test_rejects_with_reason(self):
        for q, reason in REJECTS.items():
            with self.subTest(q=q):
                with self.assertRaises(cg.QueryRejected) as ctx:
                    cg.check(q)
                self.assertEqual(ctx.exception.reason, reason)

    def test_none_is_rejected(self):
        with self.assertRaises(cg.QueryRejected):
            cg.check(None)  # type: ignore[arg-type]


class TraversalTest(unittest.TestCase):
    def test_every_traversal_passes_the_free_form_rules(self):
        for name, (cypher, _) in cg.TRAVERSALS.items():
            with self.subTest(name=name):
                cg.check(cypher)

    def test_params_must_match_exactly(self):
        cypher, params = cg.traversal("metrics_for_entity", {"entity_id": "ent_1"})
        self.assertIn("$entity_id", cypher)
        self.assertEqual(params, {"entity_id": "ent_1"})
        for name, params, reason in [("drop_all", {}, "unknown_traversal"),
                                     ("metrics_for_entity", {}, "bad_parameters"),
                                     ("metrics_for_entity", {"entity_id": "e", "limit": "1e9"}, "bad_parameters")]:
            with self.subTest(name=name, params=params):
                with self.assertRaises(cg.QueryRejected) as ctx:
                    cg.traversal(name, params)
                self.assertEqual(ctx.exception.reason, reason)


class RunReadTest(unittest.TestCase):
    def test_read_tx_timeout_and_row_cap(self):
        install_fake_neo4j()
        driver = FakeDriver(rows=500)
        rows = cg.run_read(driver, "MATCH (n) RETURN n LIMIT 50", {"a": 1})
        self.assertEqual(len(rows), cg.MAX_ROWS)
        self.assertEqual(driver.pulled, cg.MAX_ROWS)  # stops pulling; doesn't fetch everything then slice
        self.assertEqual(driver.calls[0], ("session", {"database": None, "default_access_mode": "READ"}))
        self.assertEqual(driver.calls[1], ("execute_read", cg.TIMEOUT_S))
        self.assertEqual(driver.calls[2], ("tx.run", "MATCH (n) RETURN n LIMIT 50", {"a": 1}))


class RegisterTest(unittest.TestCase):
    def test_free_form_tool_only_when_asked(self):
        install_fake_neo4j()
        end_user = FakeMCP()
        cg.register(end_user, FakeDriver())
        self.assertEqual(sorted(end_user.tools), ["graph_traversal"])

    def test_refusal_never_reaches_the_database(self):
        install_fake_neo4j()
        driver, server = FakeDriver(rows=3), FakeMCP()
        cg.register(server, driver, free_form=True)
        with self.assertRaises(cg.QueryRejected):
            server.tools["execute_read_cypher"]("MATCH (n) WITH n CALL apoc.help('x') YIELD name RETURN name LIMIT 1")
        with self.assertRaises(cg.QueryRejected):
            server.tools["graph_traversal"]("delete_everything", {})
        self.assertEqual(driver.calls, [])
        out = server.tools["execute_read_cypher"]("MATCH (n) RETURN n LIMIT 3")
        self.assertEqual(json.loads(out), [{"i": 0}, {"i": 1}, {"i": 2}])
        server.tools["graph_traversal"]("metrics_for_entity", {"entity_id": "ent_1"})
        self.assertEqual(driver.calls[-1][2], {"entity_id": "ent_1"})
        server.tools["execute_read_cypher"]("MATCH (n {id: $id}) RETURN n LIMIT 1", {"id": "x; DROP"})
        self.assertIn(("tx.run", "MATCH (n {id: $id}) RETURN n LIMIT 1", {"id": "x; DROP"}), driver.calls)


class CliTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cg.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_main(["MATCH (n) RETURN n LIMIT 5"]), (0, "allowed\n"))
        code, text = self.run_main(["MATCH (n) DELETE n RETURN n LIMIT 5"])
        self.assertEqual(code, 1)
        self.assertIn("forbidden_clause", text)
        self.assertEqual(self.run_main([])[0], 2)
        self.assertEqual(self.run_main(["  "])[0], 2)


if __name__ == "__main__":
    unittest.main()

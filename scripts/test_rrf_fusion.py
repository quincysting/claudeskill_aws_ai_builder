"""Offline tests for rrf_fusion.py. No AWS, no network. Run: python -B -m unittest test_rrf_fusion"""
import unittest

from rrf_fusion import rrf


def ids(docs):
    return [d["chunk_id"] for d in docs]


def hits(*names):
    return [{"chunk_id": n} for n in names]


class RRFMaths(unittest.TestCase):
    def test_negative_top_k_refused(self):
        with self.assertRaises(ValueError):
            rrf([hits("a", "b")], top_k=-1)

    def test_k60_and_ranks_start_at_1(self):
        out = rrf([hits("a", "b"), hits("b", "c")], top_k=None)
        self.assertEqual(ids(out), ["b", "a", "c"])
        scores = {d["chunk_id"]: d["score"] for d in out}
        self.assertEqual(scores["a"], 1 / 61)            # rank 1 in one list
        self.assertEqual(scores["c"], 1 / 62)            # rank 2 in one list
        self.assertEqual(scores["b"], 1 / 62 + 1 / 61)   # rank 2 and rank 1
        self.assertTrue(all(isinstance(d["score"], float) for d in out))

    def test_two_lists_never_exceed_2_over_61(self):
        out = rrf([hits("a", "b", "c"), hits("a", "c", "b")], top_k=None)
        self.assertEqual(out[0]["score"], 2 / 61)
        self.assertTrue(all(d["score"] <= 2 / 61 for d in out))

    def test_other_k(self):
        self.assertEqual(rrf([hits("a")], k=1)[0]["score"], 1 / 2)
        self.assertEqual(rrf([hits("a")], k=0)[0]["score"], 1.0)
        with self.assertRaises(ValueError):
            rrf([hits("a")], k=-1)

    def test_tie_keeps_first_seen_order(self):
        out = rrf([hits("a", "b"), hits("b", "a")])
        self.assertEqual(out[0]["score"], out[1]["score"])
        self.assertEqual(ids(out), ["a", "b"])           # the first list's order breaks the tie
        self.assertEqual(ids(rrf([hits("b", "a"), hits("a", "b")])), ["b", "a"])

    def test_three_list_tie_is_exact_in_any_order(self):
        # x has ranks 1, 2, 4 and y has ranks 2, 4, 1: same set, summed in a different order
        lists = [hits("x", "y"), hits("f1", "x", "f2", "y"), hits("y", "f3", "f4", "x")]
        out = rrf(lists, top_k=None)
        scores = {d["chunk_id"]: d["score"] for d in out}
        self.assertEqual(scores["x"], scores["y"])
        self.assertEqual(ids(out)[:2], ["x", "y"])

    def test_duplicate_in_one_list_counts_once_at_best_rank(self):
        out = rrf([hits("a", "a", "b")], top_k=None)
        scores = {d["chunk_id"]: d["score"] for d in out}
        self.assertEqual(scores["a"], 1 / 61)            # not 1/61 + 1/62
        self.assertEqual(scores["b"], 1 / 62)            # the dropped copy doesn't push b down
        both = rrf([hits("a", "a", "b"), hits("a", "b", "b")], top_k=None)
        self.assertEqual(both[0]["score"], 2 / 61)

    def test_duplicates_across_lists_merge(self):
        out = rrf([hits("a"), hits("a")], top_k=None)
        self.assertEqual(len(out), 1)


class RRFContract(unittest.TestCase):
    def test_top_k(self):
        lists = [hits("a", "b", "c", "d")]
        self.assertEqual(ids(rrf(lists)), ["a", "b", "c"])     # default 3
        self.assertEqual(ids(rrf(lists, top_k=1)), ["a"])
        self.assertEqual(len(rrf(lists, top_k=None)), 4)

    def test_empty_inputs(self):
        self.assertEqual(rrf([]), [])
        self.assertEqual(rrf([[], []]), [])

    def test_custom_key_first_copy_kept_and_raw_score_replaced(self):
        bm25 = [{"id": "a", "text": "from bm25", "score": 12.3}]
        knn = [{"id": "a", "text": "from knn", "score": 0.81}]
        out = rrf([bm25, knn], key="id")
        self.assertEqual(out[0]["text"], "from bm25")
        self.assertEqual(out[0]["score"], 2 / 61)

    def test_inputs_not_mutated(self):
        bm25 = [{"chunk_id": "a", "score": 7.0}]
        rrf([bm25])
        self.assertEqual(bm25, [{"chunk_id": "a", "score": 7.0}])

    def test_missing_key_fails_loudly(self):
        with self.assertRaises(KeyError):
            rrf([[{"other": "a"}]])


if __name__ == "__main__":
    unittest.main()

"""问数场景的来源、答案和完整结果协议。"""
from copy import deepcopy
import unittest

from fuju_rsi.ask_data import AskDataError, build_casebook, evaluate
from fuju_rsi.core import Prediction


def result(rows=None, *, ordered=False, tolerance=0, columns=None):
    return {"behavior": "answer", "requiresDelivery": False,
            "result": {"columns": ["amount"] if columns is None else columns,
                       "rows": [[100], [100]] if rows is None else rows,
                       "ordered": ordered, "absoluteTolerance": tolerance}}


def actual(rows=None, *, complete=True, columns=None):
    return Prediction({"status": "succeeded", "behavior": "answer",
                       "result": {"columns": ["amount"] if columns is None else columns,
                                  "rows": [[100], [100]] if rows is None else rows,
                                  "complete": complete}})


def catalog():
    return {
        "schemaVersion": 1, "role": "development", "id": "ask-sales",
        "name": "销售问数", "environment": {"dataSnapshot": "fixture-1",
                                  "contextRevision": "metric-1"},
        "sources": [
            {"id": "source-a", "kind": "user-case", "ref": "ticket-001",
             "familyId": "family-a", "split": "train"},
            {"id": "source-b", "kind": "user-case", "ref": "ticket-002",
             "familyId": "family-b", "split": "validation"},
        ],
        "cases": [
            {"id": "case-a", "sourceId": "source-a", "question": "销售额是多少",
             "tags": ["金额"], "status": "ready", "expected": result(),
             "oracle": {"verified": True, "evidence": "独立核算的 fixture-1 总额",
                        "method": "independent-calculation", "dataSnapshot": "fixture-1",
                        "contextRevision": "metric-1"}},
            {"id": "case-b", "sourceId": "source-b", "question": "退款是多少",
             "tags": ["金额"], "status": "ready", "expected": result([[10]]),
             "oracle": {"verified": True, "evidence": "独立核算的退款金额",
                        "method": "reference-sql", "dataSnapshot": "fixture-1",
                        "contextRevision": "metric-1"}},
        ],
    }


class AskDataCatalogTests(unittest.TestCase):
    def test_source_assigns_group_and_split_before_variants(self):
        book = build_casebook(catalog())
        self.assertEqual(book["role"], "development")
        self.assertEqual(book["cases"][0]["group"], "family-a")
        self.assertEqual(book["cases"][1]["split"], "validation")
        self.assertEqual(book["cases"][0]["source"]["ref"], "ticket-001")
        self.assertEqual(book["cases"][0]["expectedStatus"], "verified")
        changed = catalog()
        changed["cases"].append({
            "id": "case-a-variant", "sourceId": "source-a", "origin": "variant",
            "parentCaseIds": ["case-a"], "question": "请问销售额", "tags": ["改写"]})
        variant = build_casebook(changed)["cases"][-1]
        self.assertEqual((variant["group"], variant["split"], variant["source"]["ref"]),
                         ("family-a", "train", "ticket-001"))
        self.assertEqual(variant["expectedStatus"], "draft")

    def test_case_cannot_change_source_split_or_family(self):
        for override in ({"split": "validation"}, {"group": "new-family"}):
            changed = catalog()
            changed["cases"][0].update(override)
            with self.subTest(override=override), self.assertRaises(AskDataError):
                build_casebook(changed)

    def test_same_family_cannot_cross_splits(self):
        changed = catalog()
        changed["sources"][1]["familyId"] = "family-a"
        with self.assertRaises(AskDataError):
            build_casebook(changed)

    def test_ready_requires_independent_evidence_and_matching_versions(self):
        for edit in (("verified", False), ("evidence", ""), ("dataSnapshot", "fixture-2"),
                     ("contextRevision", "metric-2")):
            changed = catalog()
            changed["cases"][0]["oracle"][edit[0]] = edit[1]
            with self.subTest(edit=edit), self.assertRaises(AskDataError):
                build_casebook(changed)

    def test_parent_must_be_real_case_from_same_source(self):
        changed = catalog()
        changed["cases"][1].update(origin="variant", parentCaseIds=["case-a"])
        with self.assertRaises(AskDataError):
            build_casebook(changed)

    def test_parent_cycle_and_duplicate_source_reference_are_rejected(self):
        changed = catalog()
        changed["cases"][0].update(origin="variant", parentCaseIds=["case-a-next"])
        changed["cases"].append({"id": "case-a-next", "sourceId": "source-a",
                                 "origin": "variant", "parentCaseIds": ["case-a"],
                                 "question": "另一种说法", "tags": ["改写"]})
        with self.assertRaisesRegex(AskDataError, "循环"):
            build_casebook(changed)
        changed = catalog()
        changed["sources"][1]["ref"] = "ticket-001"
        changed["sources"][1]["kind"] = "another-kind"
        with self.assertRaisesRegex(AskDataError, "重复"):
            build_casebook(changed)

    def test_holdout_is_a_separate_role_and_only_test_split(self):
        changed = catalog()
        changed["role"] = "holdout"
        with self.assertRaises(AskDataError):
            build_casebook(changed)
        changed["sources"] = [deepcopy(changed["sources"][0])]
        changed["sources"][0]["split"] = "test"
        changed["cases"] = [deepcopy(changed["cases"][0])]
        self.assertEqual(build_casebook(changed)["cases"][0]["split"], "test")

    def test_multiturn_keeps_each_turn_expectation(self):
        changed = catalog()
        changed["cases"][0].pop("question")
        changed["cases"][0].pop("expected")
        changed["cases"][0]["turns"] = [{"question": "先查 A", "expected": result([[1]])},
                                         {"question": "改成 B", "expected": result([[2]])}]
        book = build_casebook(changed)
        self.assertEqual(book["cases"][0]["input"], {"turns": ["先查 A", "改成 B"]})
        self.assertEqual(len(book["cases"][0]["expected"]["turns"]), 2)


class AskDataEvaluationTests(unittest.TestCase):
    def test_unordered_result_preserves_duplicate_rows(self):
        self.assertEqual(evaluate(result(), actual()).score, 1.0)
        self.assertEqual(evaluate(result(), actual([[100], [101]])).score, 0.0)

    def test_order_and_tolerance_are_explicit(self):
        self.assertEqual(evaluate(result([[1], [2]]), actual([[2], [1]])).score, 1.0)
        self.assertEqual(evaluate(result([[1], [2]], ordered=True),
                                  actual([[2], [1]])).score, 0.0)
        self.assertEqual(evaluate(result([[1.0]], tolerance=0.02),
                                  actual([[1.01]])).score, 1.0)
        self.assertEqual(evaluate(result([[1.0]], tolerance=0.001),
                                  actual([[1.01]])).score, 0.0)
        self.assertEqual(evaluate(result([[1]]), actual([[True]])).score, 0.0)

    def test_empty_set_requires_columns_and_complete_result(self):
        expected = result([], columns=["order_id", "amount"])
        self.assertEqual(evaluate(expected, actual([], columns=["order_id", "amount"])).score, 1.0)
        self.assertEqual(evaluate(expected, actual([], columns=[])).score, 0.0)
        with self.assertRaises(AskDataError):
            evaluate(expected, Prediction({"status": "succeeded", "behavior": "answer",
                                          "result": {"rows": []}}))
        self.assertEqual(evaluate(expected, actual([], complete=False,
                                                   columns=["order_id", "amount"])).score, 0.0)

    def test_failure_or_malformed_result_never_passes_as_business_answer(self):
        with self.assertRaises(AskDataError):
            evaluate(result(), Prediction({"status": "failed", "behavior": "answer"}))
        with self.assertRaises(AskDataError):
            evaluate(result(), Prediction({"status": "succeeded", "behavior": "answer",
                                          "result": {"columns": ["amount"], "rows": []}}))

    def test_delivery_and_behavior_require_independent_judges(self):
        expected = result()
        expected.pop("requiresDelivery")
        with self.assertRaises(AskDataError):
            evaluate(expected, actual())
        self.assertEqual(evaluate(expected, actual(),
                                  delivery_judge=lambda _expected, _actual: False).score, 0.0)
        self.assertEqual(evaluate(expected, actual(),
                                  delivery_judge=lambda _expected, _actual: True).score, 1.0)
        clarify = {"behavior": "clarify", "rubric": "询问具体时间范围",
                   "basisRefs": ["rule-1"]}
        observed = Prediction({"status": "succeeded", "behavior": "clarify",
                               "answer": "请问查询哪个时间范围？"})
        with self.assertRaises(AskDataError):
            evaluate(clarify, observed)
        self.assertEqual(evaluate(clarify, observed,
                                  behavior_judge=lambda _expected, _actual: True).score, 1.0)

    def test_multiturn_is_all_or_nothing(self):
        expected = {"turns": [result([[1]]), result([[2]])]}
        passed = {"turns": [actual([[1]]).output, actual([[2]]).output]}
        failed = deepcopy(passed)
        failed["turns"][1]["result"]["rows"] = [[3]]
        self.assertEqual(evaluate(expected, Prediction(passed)).score, 1.0)
        verdict = evaluate(expected, Prediction(failed))
        self.assertEqual(verdict.score, 0.0)
        self.assertIn("第 2 轮", verdict.reason)
        with self.assertRaises(AskDataError):
            evaluate(expected, Prediction({"turns": passed["turns"][:1]}))


if __name__ == "__main__":
    unittest.main()

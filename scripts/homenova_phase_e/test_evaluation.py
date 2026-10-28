"""Offline checks of transport classification, session isolation and numeric review."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


def module(name):
    spec = importlib.util.spec_from_file_location("homenova_e_test_" + name, HERE / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


RUN = module("run_agent_tests")
EVAL = module("evaluate")


class OfflineEvaluationTests(unittest.TestCase):
    def test_plan_has_ten_questions_and_two_isolated_q09_sessions(self):
        plan = RUN.make_plan()
        self.assertEqual(len(plan), 11)
        self.assertEqual(len({row["question_id"] for row in plan}), 10)
        pair = [row for row in plan if row["question_id"] == "q09"]
        self.assertEqual(len({row["prompt_sha256"] for row in pair}), 1)
        self.assertEqual(len({row["session_id"] for row in pair}), 2)
        self.assertEqual(len({row["datasource_id"] for row in pair}), 2)
        self.assertTrue(all(len(row["prompt"]) <= 500 for row in plan))
        for row in plan:
            self.assertNotIn("SELECT ", row["prompt"].upper())
            self.assertNotIn("expected_results", row)

    def test_http_200_is_not_an_agent_success(self):
        self.assertTrue(RUN.classify(200, {"status": "failed"}).startswith("agent_failed"))
        self.assertEqual(RUN.classify(200, {"status": "waiting_clarification"}), "waiting_clarification")
        self.assertEqual(RUN.classify(502, {"status": "completed"}), "http_failure")

    def example(self):
        row = {"month": "2026-03-01", "paid_orders": 10, "gmv_gross_cents": 10000,
               "goods_aov_gross_cents": 1000, "net_sales_ex_vat_cents": 7000, "goods_profit_ex_vat_cents": 3000}
        reference = {"columns": list(row), "rows": [dict(row)]}
        case = {"case_id": "q01_test", "question_id": "q01", "snapshot": "asof_2026_08_31",
                "status": "executed_requires_independent_review", "http_status": 200,
                "selected_snapshot_verified": True,
                "response": {"status": "completed", "route": "database_query", "rows": [dict(row)],
                             "execution_log": [{"stage": "execute_duckdb", "success": True}]}}
        return case, reference

    def test_numerical_agreement_still_requires_semantic_review(self):
        case, reference = self.example()
        result = EVAL.assess(case, reference)
        self.assertEqual(result["numeric"]["status"], "required_numeric_metrics_match")
        self.assertEqual(result["verdict"], "numeric_pass_semantics_need_review")
        self.assertEqual(result["business_semantics"]["status"], "manual_review_required")

    def test_one_cent_error_is_detected_despite_completed_status(self):
        case, reference = self.example()
        case["response"]["rows"][0]["gmv_gross_cents"] += 1
        result = EVAL.assess(case, reference)
        self.assertEqual(result["verdict"], "numeric_failed")
        self.assertGreater(result["numeric"]["mismatch_count"], 0)

    def test_unknown_aliases_are_deferred_not_falsely_passed(self):
        case, reference = self.example()
        case["response"]["rows"] = [{"unrecognized_layout": 1}]
        self.assertEqual(EVAL.numeric_review(case, reference)["status"], "needs_manual_mapping")

    def test_q05_chinese_labels_map_to_reference_and_comparison(self):
        expected = [
            {"label": "reference", "sku_id": "PLG-MINI", "paid_units": 10, "realized_unit_gross_cents": 1999,
             "gmv_gross_cents": 19990, "cohort_goods_profit_ex_vat_cents": 10000},
            {"label": "comparison", "sku_id": "PLG-MINI", "paid_units": 20, "realized_unit_gross_cents": 1699,
             "gmv_gross_cents": 33980, "cohort_goods_profit_ex_vat_cents": 12000},
        ]
        actual = [
            {"时期": "基准期5月25日—6月7日", "SKU": "PLG-MINI", "销量": 10, "成交单价": 1999,
             "商品GMV": 19990, "商品毛利": 10000},
            {"时期": "促销期6月8日—21日", "SKU": "PLG-MINI", "销量": 20, "成交单价": 1699,
             "商品GMV": 33980, "商品毛利": 12000},
        ]
        case = {"question_id": "q05", "response": {"rows": actual}}
        reference = {"columns": list(expected[0]), "rows": expected}
        self.assertEqual(EVAL.numeric_review(case, reference)["status"], "required_numeric_metrics_match")
        actual[1]["商品GMV"] += 1
        self.assertEqual(EVAL.numeric_review(case, reference)["status"], "reported_numeric_mismatch")

    def test_unresolved_dimension_labels_are_manual_not_numeric_fail(self):
        case, reference = self.example()
        case["response"]["rows"][0]["month"] = "不明确的时期"
        self.assertEqual(EVAL.numeric_review(case, reference)["status"], "partial_or_needs_manual_review")

    def test_failed_answer_can_preserve_valid_sql_evidence_without_passing(self):
        case, reference = self.example()
        case["status"] = "agent_failed:response_finalize"
        case["response"]["status"] = "failed"
        result = EVAL.assess(case, reference)
        self.assertEqual(result["numeric"]["status"], "required_numeric_metrics_match")
        self.assertTrue(result["verdict"].startswith("agent_failed"))

    def test_redaction_handles_nested_and_json_escaped_secrets(self):
        password = 'not-a-real-"password"'
        token = "not-a-real-token-1234567890"
        cleaned = RUN.clean({"error": ["password=" + password, {"authorization": "Bearer " + token}]}, (password, token))
        self.assertNotIn(password, str(cleaned))
        self.assertNotIn(token, str(cleaned))

    def test_refusal_keywords_do_not_automatically_pass_counterfactual_question(self):
        case, _ = self.example()
        case.update(case_id="q10_test", question_id="q10")
        case["response"].update(rows=[], analysis="无法证明因果，不能确定精确失单。")
        result = EVAL.assess(case, {"columns": ["sku_id"], "rows": [{"sku_id": "PLG-MINI"}]})
        self.assertEqual(result["verdict"], "needs_manual_review")
        self.assertEqual(result["evidence_boundary"]["status"], "manual_review_required")


if __name__ == "__main__":
    unittest.main(verbosity=2)

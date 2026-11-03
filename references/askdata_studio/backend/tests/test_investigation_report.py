import copy
import json
import unittest
from types import SimpleNamespace

from app.querying.data_qa_agent import DataQaAgent
from app.skills import SkillRegistry


class InvestigationReportTest(unittest.TestCase):
    def test_report_reads_results_without_inheriting_investigator_claims(self):
        context = {"evidence": self.evidence, "measurement_memory": {"old_summary": "过期数值"},
            "feedback": "上一轮审核的旧解释", "progress": {
            "explanations": [{"status": "supported", "text": "库存已证实造成全部损失"}],
            "facts": [{"text": "旧摘要里的错误数值"}],
            "core_questions": [{"question": "为什么下降", "id": "Q1", "status": "blocked", "reason": "内部方法冲突"}],
            "unresolved_questions": ["库存与成交的时间关系"]}}
        before = copy.deepcopy(context)
        captured = []
        self.agent.draft_investigation("为什么下降", context, self.scope,
            call_model=lambda _, payload: (captured.append(payload) or {"markdown": "当前已确认下降[R1]"}))
        self.assertEqual(context, before)
        self.assertEqual(captured[0]["context"]["evidence"][0]["rows"], self.evidence[0]["rows"])
        self.assertNotIn("measurement_memory", captured[0]["context"])
        self.assertNotIn("feedback", captured[0]["context"])
        self.assertNotIn("explanations", captured[0]["context"]["progress"])
        self.assertNotIn("facts", captured[0]["context"]["progress"])
        self.assertEqual(captured[0]["context"]["progress"]["core_questions"], [{"question": "为什么下降"}])

    def setUp(self):
        self.agent = DataQaAgent(None, self.no_tools, SkillRegistry().get("data_qa"))
        self.scope = {"user_id": "u1", "task_id": "t1", "datasource_id": "s1"}
        self.evidence = [
            {"id": "R1", "version": "v1", "user_id": "u1", "task_id": "t1", "datasource_id": "s1",
             "goal": "两期GMV", "check_status": "basic_valid", "complete": True, "stored_rows": 2,
             "total_rows": 2, "columns": ["month", "gmv"],
             "rows": [{"month": "2026-06", "gmv": 100}, {"month": "2026-07", "gmv": 80}],
             "column_metadata": {"gmv": {"unit": "EUR", "currency": "EUR", "precision": 2}}},
            {"id": "R2", "version": "v2", "task_id": "t1", "datasource_id": "s1", "goal": "商品贡献",
             "check_status": "basic_valid", "complete": True, "total_rows": 2, "stored_rows": 2,
             "columns": ["sku", "change"], "rows": [{"sku": "A", "change": -30}, {"sku": "B", "change": 10}]},
        ]

    @staticmethod
    def no_tools(*args):
        raise AssertionError("调查草稿不应重新查询、路由或调用展示MCP")

    def draft(self, payload=None, evidence=None):
        calls = []
        response = payload or {"action": "report", "title": "GMV调查", "answer": "下降20%",
                               "markdown": "GMV下降20%，A商品贡献-30，B抵消10。[R1][R2]",
                               "visualizations": [self.chart()]}

        def model(system, body):
            calls.append((system, body))
            return response

        result = self.agent.draft_investigation("7月GMV为什么少了？", {"evidence": evidence or self.evidence},
                                               self.scope, call_model=model)
        return result, calls

    @staticmethod
    def chart(**overrides):
        return {"type": "bar", "title": "两期GMV", "source_result_id": "R1", "source_version": "v1",
                "category_field": "month", "value_field": "gmv", "order": "category_asc", **overrides}

    def test_same_component_forces_report_in_one_call_without_report_keyword(self):
        result, calls = self.draft()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["mode"], "investigation_report")
        self.assertIn("两期GMV", str(calls[0][1]))
        self.assertEqual(result.action, "report")
        self.assertEqual([source.result_id for source in result.report.data_sources], ["R1", "R2"])
        self.assertEqual(result.report.visualizations[0].source_result_id, "R1")
        self.assertEqual(result.report.visualizations[0].currency, "EUR")
        self.assertEqual(result.report.visualizations[0].order, "category_asc")
        self.assertEqual(result.report.visualizations[0].source_task_id, "t1")

    def test_sources_are_backend_data_not_model_values(self):
        result, _ = self.draft({"draft": "GMV下降。[R1]", "data_sources": [{"rows": [{"gmv": 999}]}]})
        self.assertEqual(result.report.data_sources[0].rows[0]["gmv"], 100)
        tampered = result.report.model_dump()
        tampered["data_sources"][0]["rows"][0]["gmv"] = 999
        with self.assertRaisesRegex(ValueError, "版本或数据"):
            self.agent.validate_investigation_report(tampered, self.evidence, self.scope)

    def test_old_draft_output_stays_usable_without_extra_call(self):
        result, calls = self.draft({"draft": "已有证据支持下降。[R1]"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.report.visualizations, [])

    def test_partial_preview_omits_chart_instead_of_claiming_full_result(self):
        evidence = copy.deepcopy(self.evidence)
        evidence[0].update(total_rows=300, stored_rows=300, display_truncated=True)
        result, _ = self.draft(evidence=evidence)
        self.assertEqual(result.report.visualizations, [])
        self.assertIn("仅提供部分结果", result.warnings[0])
        self.assertFalse(result.report.data_sources[0].complete)

    def test_invalid_result_or_version_does_not_fall_back_to_task(self):
        for overrides in ({"source_result_id": "R9"}, {"source_version": "old"}, {"source_task_id": "other"}):
            with self.subTest(overrides=overrides):
                result, calls = self.draft({"draft": "事实[R1]", "visualizations": [self.chart(**overrides)]})
                self.assertEqual(result.report.visualizations, [])
                self.assertEqual(len(calls), 1)
                self.assertTrue(result.warnings)
                untrusted = result.report.model_dump()
                untrusted["visualizations"] = [self.chart(**overrides)]
                with self.assertRaises(ValueError):
                    self.agent.validate_investigation_report(untrusted, self.evidence, self.scope)

    def test_unauthorized_user_task_source_or_snapshot_rejected(self):
        for field, value in (("user_id", "u2"), ("task_id", "t2"), ("datasource_id", "s2")):
            evidence = copy.deepcopy(self.evidence)
            evidence[0][field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.draft(evidence=evidence)
        evidence = copy.deepcopy(self.evidence)
        evidence[0]["snapshot"] = {"as_of": "2026-09-30"}
        with self.assertRaises(PermissionError):
            self.agent._investigation_sources(evidence, {**self.scope, "snapshot": {"as_of": "2026-08-31"}})

    def test_citations_must_point_to_available_result(self):
        with self.assertRaisesRegex(ValueError, "不存在的结果"):
            self.draft({"draft": "事实[R999]"})
        with self.assertRaisesRegex(ValueError, "过期结果版本"):
            self.draft({"draft": "事实[R1@old]"})

    def test_short_citation_binds_exactly_and_foreign_or_unknown_ids_are_rejected(self):
        evidence = copy.deepcopy(self.evidence)
        evidence[0]["id"] = "a12b:R1"
        result, _ = self.draft({"draft": "事实[a12b:R1@v1]"}, evidence)
        self.assertIn("[a12b:R1@v1]", result.report.markdown)
        short, _ = self.draft({"draft": "事实[R1]"}, evidence)
        self.assertIn("[a12b:R1]", short.report.markdown)
        for citation in ("other:R1", "a12b:R99"):
            with self.subTest(citation=citation), self.assertRaisesRegex(ValueError, "不存在的结果"):
                self.draft({"draft": f"事实[{citation}]"}, evidence)
        with self.assertRaisesRegex(ValueError, "过期结果版本"):
            self.draft({"draft": "事实[a12b:R1@old]"}, evidence)

    def test_invalid_reference_gets_one_local_repair_with_previous_output(self):
        outputs = iter([{"draft": "下降[typo:R1]"}, {"draft": "下降[R1]"}])
        calls = []
        def model(system, payload):
            calls.append(copy.deepcopy(payload))
            return next(outputs)
        result = self.agent.draft_investigation("变化", {"evidence": self.evidence}, self.scope, call_model=model)
        self.assertEqual(result.report.markdown, "下降[R1]")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["reference_repair"]["previous_output"], {"draft": "下降[typo:R1]"})
        self.assertIn("typo:R1", calls[1]["reference_repair"]["error"])

    def test_short_alias_is_unavailable_for_colliding_result_ids(self):
        sources = [SimpleNamespace(result_id="A:R1"), SimpleNamespace(result_id="B:R1")]
        self.assertEqual(self.agent._citation_aliases(sources), {"A:R1":"A:R1", "B:R1":"B:R1"})

    def test_chart_fields_and_units_are_checked_against_actual_evidence(self):
        for overrides in ({"value_field": "unknown"}, {"currency": "CNY"}, {"unit": "人民币"}):
            with self.subTest(overrides=overrides):
                result, _ = self.draft({"draft": "事实[R1]", "visualizations": [self.chart(**overrides)]})
                self.assertEqual(result.report.visualizations, [])
                untrusted = result.report.model_dump()
                untrusted["visualizations"] = [self.chart(**overrides)]
                with self.assertRaises(ValueError):
                    self.agent.validate_investigation_report(untrusted, self.evidence, self.scope)

    def test_pie_cannot_hide_negative_contribution(self):
        chart = self.chart(type="pie", source_result_id="R2", source_version="v2", category_field="sku", value_field="change")
        result, _ = self.draft({"draft": "贡献[R2]", "visualizations": [chart]})
        self.assertEqual(result.report.visualizations, [])
        untrusted = result.report.model_dump()
        untrusted["visualizations"] = [chart]
        with self.assertRaisesRegex(ValueError, "负值"):
            self.agent.validate_investigation_report(untrusted, self.evidence, self.scope)

    def test_pie_cannot_renormalize_selected_categories(self):
        evidence = copy.deepcopy(self.evidence)
        evidence[0].update(total_rows=4, stored_rows=4)
        evidence[0]["rows"].extend([{"month": "2026-08", "gmv": 60}, {"month": "2026-09", "gmv": 50}])
        result, _ = self.draft({"draft": "占比[R1]", "visualizations": [self.chart(type="pie", max_items=3)]}, evidence)
        self.assertEqual(result.report.visualizations, [])
        untrusted = result.report.model_dump()
        untrusted["visualizations"] = [self.chart(type="pie", max_items=3)]
        with self.assertRaisesRegex(ValueError, "截取部分分类"):
            self.agent.validate_investigation_report(untrusted, evidence, self.scope)

    def test_bad_optional_chart_preserves_valid_text_and_other_chart_without_retry(self):
        result, calls = self.draft({"draft": "GMV下降。[R1]", "visualizations": [
            self.chart(), {"title": "缺字段"}, self.chart(value_field="missing")],
            "evidence_gaps": ["价格原因尚缺证据"]})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result.report.visualizations), 1)
        self.assertEqual(len(result.warnings), 2)
        self.assertEqual(result.report.markdown, "GMV下降。[R1]")
        self.assertEqual(result.evidence_gaps, ["价格原因尚缺证据"])
        self.assertNotIn("价格原因尚缺证据", result.report.warnings)

    def test_validation_after_review_is_idempotent_and_detects_changes(self):
        result, _ = self.draft()
        checked = self.agent.validate_investigation_report(result.report, self.evidence, self.scope)
        self.assertEqual(checked, result.report)
        changed = copy.deepcopy(self.evidence)
        changed[0]["version"] = "v3"
        with self.assertRaises(ValueError):
            self.agent.validate_investigation_report(result.report, changed, self.scope)

    def test_original_data_qa_keeps_answer_mode(self):
        client = SimpleNamespace(list_tools=lambda: [])
        model = SimpleNamespace(chat_json=lambda *args: {"action": "answer", "answer": "已有结果中的GMV为80", "tool_calls": []})
        agent = DataQaAgent(model, lambda scope: client, SkillRegistry().get("data_qa"))
        result = agent.run("刚才GMV是多少？", {}, self.scope)
        self.assertEqual(result.action, "answer")
        self.assertIsNone(result.report)

    def test_original_data_qa_can_still_use_presentation_tool(self):
        calls = []

        def tool(name, arguments):
            calls.append((name, arguments))
            return {"type": "bar", **arguments}

        client = SimpleNamespace(list_tools=lambda: [{"name": "build_bar_chart"}], call_tool=tool)
        model = SimpleNamespace(chat_json=lambda *args: {
            "action": "report", "title": "对比", "markdown": "两期对比", "tool_calls": [{
                "name": "build_bar_chart", "arguments": {"title": "对比", "source_task_id": "old-task",
                                                         "category_field": "month", "value_field": "gmv"}}]})
        agent = DataQaAgent(model, lambda scope: client, SkillRegistry().get("data_qa"))
        contexts = {"selected_tables": json.dumps([{**self.evidence[0], "task_id": "old-task"}])}
        result = agent.run("用刚才的结果写报告", contexts, self.scope)
        self.assertEqual(result.action, "report")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.report.visualizations[0].source_task_id, "old-task")


if __name__ == "__main__":
    unittest.main()

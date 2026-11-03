import base64
import copy
import json
import re
import unittest
from app.foundry_adapter.report_presentation import present_report


class ReportPresentationTest(unittest.TestCase):
    def test_partial_report_keeps_agent_conclusion_and_closing_without_extra_footer(self):
        from app.foundry_adapter.projection import result_text
        result = {'status': 'partial', 'report': {'markdown': '## 当前结论\n订单减少。\n\n## 后续关注\n原因需要进一步区分。',
                  'warnings': ['内部查询诊断']}, 'stop_reason': '取证轮数已用完'}
        text = result_text(result)
        self.assertIn('当前结论', text)
        self.assertIn('原因需要进一步区分', text)
        self.assertNotIn('分析尚未完成', text)
        self.assertNotIn('内部查询诊断', text)
        self.assertNotIn('取证轮数已用完', text)

    def setUp(self):
        self.report = {'markdown': '销售变化需继续调查。[t:R1]\n\n<!-- chart:t:R1:gmv -->\n\n## 未完成\n原因待查。',
            'data_sources': [{'result_id': 't:R1', 'version': 'v1', 'complete': True, 'title': '实际查询',
                'columns': ['month', 'gmv'], 'rows': [{'month': 'June', 'gmv': 12000}, {'month': 'July', 'gmv': 9000}],
                'units': {'gmv': 'EUR cents'}, 'total_rows': 2}],
            'visualizations': [{'type': 'bar', 'title': '月度对比', 'source_result_id': 't:R1', 'source_version': 'v1',
                'category_field': 'month', 'value_field': 'gmv', 'unit': 'EUR cents', 'max_items': 12}]}

    def test_projection_binds_chart_to_rows_and_keeps_agent_narrative(self):
        before = copy.deepcopy(self.report)
        result = present_report(self.report)
        encoded = re.search(r'<!-- homenova-chart:(.*?) -->', result)[1]
        chart = json.loads(base64.b64decode(encoded))
        self.assertEqual(chart['points'], [{'label': 'June', 'value': 12000}, {'label': 'July', 'value': 9000}])
        self.assertEqual(chart['unit'], 'EUR cents')
        self.assertIn('销售变化需继续调查。[查看明细](#report-source-t%3AR1)', result)
        self.assertLess(result.index('原因待查'), result.index('homenova-report-appendix'))
        self.assertIn('homenova-source:t%3AR1', result)
        self.assertEqual(self.report, before)

    def test_rejected_chart_cannot_render_via_remaining_placeholder(self):
        self.report['visualizations'] = []
        self.report['markdown'] += '\n<!-- homenova-chart:ZmFrZQ== -->'
        result = present_report(self.report)
        self.assertNotIn('<!-- chart:', result)
        self.assertNotIn('<!-- homenova-chart:', result)
        self.assertIn('销售变化需继续调查', result)

    def test_comparison_labels_keep_month_and_category_together(self):
        source=self.report['data_sources'][0]
        source['columns'].append('channel')
        for row in source['rows']: row['channel']='Organic'
        self.report['visualizations'][0].update(category_field='channel',series_field='month')
        result=present_report(self.report)
        encoded=re.search(r'<!-- homenova-chart:(.*?) -->',result)[1]
        chart=json.loads(base64.b64decode(encoded))
        self.assertEqual([p['label'] for p in chart['points']],['Organic · June','Organic · July'])

    def test_partial_or_stale_source_does_not_become_complete_chart(self):
        for change in ({'complete': False}, {'version': 'v2'}):
            with self.subTest(change=change):
                report = copy.deepcopy(self.report)
                report['data_sources'][0].update(change)
                self.assertNotIn('<!-- homenova-chart:', present_report(report))

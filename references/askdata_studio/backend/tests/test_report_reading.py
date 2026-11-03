import copy
import unittest
from app.models import ReportDataSource
from app.querying.report_reading import reading_view, display_number


class ReportReadingTest(unittest.TestCase):
    def source(self, **extra):
        return ReportDataSource(result_id='T:R1', version='v1', complete=True, total_rows=2,
            columns=['month','channel','gmv'], units={'gmv':'EUR cents'},
            rows=[{'month':'2026-06','channel':'A','gmv':123400},
                  {'month':'2026-07','channel':'A','gmv':98720}], **extra)

    def test_display_conversion_and_period_change_use_source_units(self):
        source=self.source();before=copy.deepcopy(source)
        view=reading_view([source])[0]
        self.assertEqual(view['rows'][0]['gmv'],'1,234.00 EUR')
        change=view['period_comparisons'][0]
        self.assertEqual(change['change'],'-246.80 EUR')
        self.assertEqual(change['change_rate'],'-20.00 %')
        self.assertEqual(source,before)

    def test_preview_does_not_create_complete_comparison(self):
        source=self.source();source.complete=False
        self.assertEqual(reading_view([source])[0]['period_comparisons'],[])

    def test_unknown_units_are_never_guessed(self):
        self.assertIn('单位未登记',display_number(123400))
        self.assertEqual(display_number(.0123,'ratio'),'1.23 %')

    def test_group_ratios_are_not_summed_or_averaged(self):
        source=self.source();source.columns=['month','channel','cvr'];source.units={'cvr':'ratio'}
        source.rows=[{'month':p,'channel':c,'cvr':v} for p,c,v in
            [('2026-06','A',.1),('2026-07','A',.05),('2026-06','B',.2),('2026-07','B',.3)]]
        source.total_rows=4
        changes=reading_view([source])[0]['period_comparisons']
        self.assertEqual(len(changes),2)
        self.assertEqual({r['channel'] for r in changes},{'A','B'})
        a=next(r for r in changes if r['channel']=='A')
        self.assertEqual(a['change'],'-5.00 个百分点')
        self.assertEqual(a['change_rate'],'-50.00 %')

    def test_duplicate_grains_do_not_get_silently_aggregated(self):
        source=self.source();source.rows.append(source.rows[0].copy());source.total_rows=3
        view=reading_view([source])[0]
        self.assertEqual(view['period_comparisons'],[])
        self.assertIn('comparison_unavailable',view)

if __name__=='__main__':unittest.main()


class CompactReadingTest(unittest.TestCase):
    def test_cells_and_units_survive_lossless_compaction(self):
        from app.querying.report_reading import compact_reading_view
        from copy import deepcopy
        source=[{'result_id':'T:R1','rows':[{'sku':'A','gmv':'1,234.56 EUR'},{'sku':'B','gmv':'未提供'}],
                 'period_comparisons':[{'metric':'gmv','base':'1,000.00 EUR','current':'800.00 EUR','change':'-200.00 EUR'}]}]
        before=deepcopy(source);compact=compact_reading_view(source)
        for key in ['rows','period_comparisons']:
            table=compact[0][key]
            self.assertEqual([dict(zip(table['columns'],row)) for row in table['rows']],source[0][key])
        self.assertEqual(source,before)

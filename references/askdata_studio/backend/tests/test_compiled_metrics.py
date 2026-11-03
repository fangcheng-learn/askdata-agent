import unittest
import duckdb
from app.querying.compiled_metrics import compile_paid_goods, RecipeNotCovered


def context():
    return {'database':'homenova_20260831','scenario':{'observation_cutoff_at':'2026-08-31T22:00:00Z'},
            'requested_time_expressions':['2026-06','2026-07']}


def request(**kwargs):
    return {'metric_requirements':['paid_goods_gmv_gross','paid_order_count'], 'dimensions':['month'], **kwargs}


class CompiledMetricsTest(unittest.TestCase):
    def setUp(self):
        self.db=duckdb.connect(':memory:')
        self.db.execute('CREATE TABLE payments(order_id VARCHAR, captured_at TIMESTAMPTZ, status VARCHAR)')
        self.db.execute('CREATE TABLE order_items(order_id VARCHAR, sku_id VARCHAR, quantity INTEGER, line_paid_gross_cents BIGINT)')
        self.db.execute('CREATE TABLE sku_catalog(sku_id VARCHAR, category_code VARCHAR)')
        self.db.executemany('INSERT INTO payments VALUES (?,?,?)',[
            ('A','2026-06-05T10:00:00Z','failed'),('A','2026-06-05T11:00:00Z','captured'),
            ('B','2026-06-30T22:00:00Z','captured'),('C','2026-07-03T00:00:00Z','failed')])
        self.db.executemany('INSERT INTO order_items VALUES (?,?,?,?)',[
            ('A','X',2,1000),('A','Y',1,800),('B','X',1,500),('C','Y',1,900)])
        self.db.executemany('INSERT INTO sku_catalog VALUES (?,?)',[('X','ONE'),('Y','TWO')])

    def tearDown(self):
        self.db.close()

    def run_recipe(self, query):
        candidate=compile_paid_goods(query,context())
        cur=self.db.execute(candidate['sql'])
        return [dict(zip([d[0] for d in cur.description],r)) for r in cur.fetchall()]

    def test_payment_retry_line_multiplicity_and_business_timezone(self):
        a,b=self.run_recipe(request())
        self.assertEqual((a['month'],a['paid_goods_gmv_gross'],a['paid_order_count'],a['paid_goods_quantity']),('2026-06',1800,1,3))
        self.assertEqual((b['month'],b['paid_goods_gmv_gross'],b['paid_order_count'],b['paid_goods_quantity']),('2026-07',500,1,1))
        self.assertEqual(a['goods_aov_gross'],1800)
        self.assertEqual(a['units_per_paid_order'],3)
        self.assertEqual(a['paid_unit_price_gross'],600)

    def test_category_scope_and_non_additive_distinct_orders(self):
        scoped=self.run_recipe(request(conditions={'sku_catalog.category_code':'ONE'}))
        self.assertEqual(scoped[0]['paid_goods_quantity'],2)
        grouped=self.run_recipe(request(dimensions=['month','sku_catalog.category_code']))
        june=[r for r in grouped if r['month']=='2026-06']
        self.assertEqual(sum(r['paid_goods_gmv_gross'] for r in june),1800)
        self.assertEqual(sum(r['paid_order_count'] for r in june),2)
        self.assertFalse(compile_paid_goods(request(),context())['output_metadata']['paid_order_count']['additive_across_entities'])

    def test_untrusted_values_are_literals_not_sql_fragments(self):
        result=self.run_recipe(request(conditions={'sku_catalog.category_code':"ONE') OR 1=1 --"}))
        self.assertEqual(result,[])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM order_items').fetchone()[0],4)

    def test_closed_wire_conditions_match_legacy_registered_recipe(self):
        typed={'months':['2026-07','2026-06'],'filters':[
            {'field':'sku_catalog.category_code','operator':'in','values':['ONE']}]}
        legacy={'months':['2026-06','2026-07'],'sku_catalog.category_code':'ONE'}
        a=compile_paid_goods(request(conditions=typed),context())
        b=compile_paid_goods(request(conditions=legacy),context())
        self.assertEqual(a['spec_fingerprint'],b['spec_fingerprint'])
        self.assertEqual(self.run_recipe(request(conditions=typed)),self.run_recipe(request(conditions=legacy)))

    def test_malformed_or_extra_null_condition_does_not_expand_recipe_coverage(self):
        for conditions in [{'months':['2026-06'],"months': ['2026-06']":None},
                           {'months':['2026-06'],'uncovered_dimension':None}]:
            with self.subTest(conditions=conditions),self.assertRaises(RecipeNotCovered):
                compile_paid_goods(request(conditions=conditions),context())

    def test_legacy_equality_membership_is_covered_but_other_predicates_are_not_changed(self):
        expected=self.run_recipe(request(conditions={'sku_catalog.category_code':'ONE'}))
        for value in [{'operator':'in','values':['ONE']},{'operator':'eq','value':'ONE'}]:
            self.assertEqual(self.run_recipe(request(conditions={'sku_catalog.category_code':value})),expected)
        for value in [{'operator':'!=','value':'ONE'},
                      {'operator':'in','values':['ONE'],'extra':None},
                      {'operator':'=','values':['ONE','TWO']}]:
            with self.subTest(value=value),self.assertRaises(RecipeNotCovered):
                compile_paid_goods(request(conditions={'sku_catalog.category_code':value}),context())

    def test_unsupported_requirements_are_never_silently_dropped(self):
        for change in [{'metric_requirements':['paid_goods_gmv_gross','stockout_duration']},
                       {'conditions':{'channel':'organic'}}, {'dimensions':['month','channel']},
                       {'required_output_fields':['unknown_measure']}, {'conditions':{'months':['last month']}}]:
            with self.subTest(change=change),self.assertRaises(RecipeNotCovered):
                compile_paid_goods(request(**change),context())

    def test_cutoff_and_source_are_bound(self):
        with self.assertRaises(RecipeNotCovered):
            compile_paid_goods(request(conditions={'months':['2026-09']}),context())
        wrong=context();wrong['database']='other'
        with self.assertRaises(RecipeNotCovered):compile_paid_goods(request(),wrong)

    def test_duplicate_periods_do_not_duplicate_money(self):
        self.assertEqual(self.run_recipe(request()),self.run_recipe(request(conditions={'months':['2026-07','2026-06','2026-07']})))

    def test_zero_quantity_has_unknown_unit_price_not_division_failure(self):
        self.db.execute('UPDATE order_items SET quantity=0')
        for row in self.run_recipe(request()):
            self.assertIsNone(row['paid_unit_price_gross'])

    def test_entity_scope_cannot_disappear(self):
        scoped=context();scoped['task']={'task_understanding':{'scope':{'kind':'category'}}}
        with self.assertRaises(RecipeNotCovered):compile_paid_goods(request(),scoped)

    def test_changed_registered_definition_does_not_use_old_recipe(self):
        from unittest.mock import patch
        from app.homenova import registered_metric
        def changed(database,name):
            metric=registered_metric(database,name)
            if metric and metric['id']=='paid_order_count':
                metric={**metric,'time_fields':[{'table':'orders','field':'ordered_at'}]}
            return metric
        with patch('app.homenova.registered_metric',side_effect=changed):
            with self.assertRaisesRegex(RecipeNotCovered,'requires revalidation'):
                compile_paid_goods(request(),context())

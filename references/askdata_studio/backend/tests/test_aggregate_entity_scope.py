import unittest
from app.querying.request_constraints import validate_request_constraints

class AggregateEntityScopeTest(unittest.TestCase):
    def check(self,sql):
        return validate_request_constraints(sql=sql,query_contract={'metrics':[{'output_columns':['n']}]},
            schema_graph={'tables':[{'id':'p','name':'payments'},{'id':'c','name':'catalog'}],
                'fields':[{'table_id':'p','name':'order_id'},{'table_id':'c','name':'category'}]},
            entity_resolutions=[{'kind':'entity','status':'resolved','canonical_field':'catalog.category','canonical_value':'X'}])

    def test_filtered_sibling_cannot_scope_a_preaggregated_count(self):
        result=self.check("WITH chosen AS (SELECT * FROM catalog WHERE category='X'), a AS (SELECT COUNT(*) n FROM payments) SELECT a.n FROM a CROSS JOIN chosen")
        self.assertIn('输出n的聚合输入未继承',result['errors'][0])

    def test_unreferenced_cte_cannot_filter_the_actual_count(self):
        self.assertFalse(self.check("WITH chosen AS (SELECT * FROM catalog WHERE category='X') SELECT COUNT(*) n FROM payments")['passed'])

    def test_real_join_before_aggregation_remains_available_for_business_review(self):
        result=self.check("WITH chosen AS (SELECT * FROM catalog WHERE category='X') SELECT COUNT(*) n FROM payments JOIN chosen ON payments.order_id=chosen.category")
        self.assertTrue(result['passed']);self.assertFalse(result['complete'])

    def test_correct_nested_aggregate_alias_chain(self):
        result=self.check("WITH chosen AS (SELECT * FROM catalog WHERE category='X'), a AS (SELECT COUNT(*) n FROM payments JOIN chosen ON payments.order_id=chosen.category), b AS (SELECT n FROM a) SELECT n FROM b")
        self.assertTrue(result['passed'])

    def test_conditional_measure_is_not_rejected_for_lacking_where(self):
        result=self.check("WITH chosen AS (SELECT * FROM catalog WHERE category='X') SELECT COUNT(CASE WHEN catalog.category='X' THEN payments.order_id END) n FROM payments CROSS JOIN catalog")
        self.assertTrue(result['passed'])

    def test_unknown_lineage_is_pending_not_falsely_certified(self):
        result=self.check("SELECT COUNT(*) n FROM payments")
        self.assertTrue(result['passed']);self.assertFalse(result['complete'])

    def test_union_keeps_output_position_when_branch_alias_differs(self):
        result=self.check("WITH chosen AS (SELECT * FROM catalog WHERE category='X'), a AS (SELECT COUNT(*) n FROM payments) SELECT COUNT(*) n FROM chosen UNION ALL SELECT a.n AS different_name FROM a")
        self.assertFalse(result['passed'])

    def test_scoped_union_branches_and_null_measure_are_not_rejected(self):
        result=self.check("WITH chosen AS (SELECT * FROM catalog WHERE category='X') SELECT COUNT(*) n FROM chosen UNION ALL SELECT NULL AS different_name FROM chosen")
        self.assertTrue(result['passed'])

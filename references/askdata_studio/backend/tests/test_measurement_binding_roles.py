from copy import deepcopy
import unittest
from app.homenova import schema_payload
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.querying.measurement_context import measurement_lineage
from app.security import AccessController


class MeasurementBindingRoleTest(unittest.TestCase):
    def setUp(self):
        self.source='homenova_20260831'
        self.scope=AccessController().scoped('demo_admin',self.source)
        self.graph=deepcopy(schema_payload(self.source))
        self.graph['fields']=[{**f,'sql_name':t['name']+'.'+f['name'],'table_id':t['id']}
                              for t in self.graph['tables'] for f in t['fields']]
        self.validator=QueryContractValidator(DuckDbEngine())
        self.sql="SELECT s.source_channel, COUNT(DISTINCT p.order_id) AS by_channel_orders FROM payments p JOIN orders o ON p.order_id=o.order_id JOIN sessions s ON o.checkout_session_id=s.session_id WHERE p.status='captured' GROUP BY s.source_channel"
        self.contract={'metrics':[{'name':'paid_order_count','source_fields':['payments.order_id','sessions.source_channel'],
                                   'aggregation':'count_distinct','output_columns':['by_channel_orders']}]}

    def validate(self,sql):
        return self.validator.validate(self.contract,sql,'按渠道查订单数',self.source,self.graph,self.scope.public())

    def test_context_dependency_does_not_change_measure_value_and_output_alias_is_bound(self):
        result=self.validate(self.sql)
        self.assertTrue(any(p['code']=='metric_context_dependency' for p in result['validation']['pending_checks']))
        self.assertTrue(any(b['output_aliases']==['by_channel_orders'] for b in result['validation']['metric_output_bindings']))

    def test_another_output_using_dimension_as_value_does_not_change_this_metric(self):
        self.validate(self.sql.replace('AS by_channel_orders FROM',
            'AS by_channel_orders, COUNT(DISTINCT s.source_channel) AS diagnostic_channel_count FROM'))

    def test_real_wrong_measure_source_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError,'指标来源超出'):
            self.validate(self.sql.replace('COUNT(DISTINCT p.order_id)','COUNT(DISTINCT s.source_channel)'))

    def test_same_name_or_unrelated_extra_column_never_establishes_allowed_context(self):
        self.contract['metrics'][0]['source_fields'].append('sessions.visitor_id')
        with self.assertRaisesRegex(ValueError,'指标来源超出'):
            self.validate(self.sql)

    def test_registered_metric_binds_arbitrary_actual_alias_without_renaming_business_id(self):
        result=self.validate(self.sql)
        from app.homenova import registered_metric
        lineage=measurement_lineage(self.sql,result,[registered_metric(self.source,'paid_order_count')],self.graph,
                                    declared_metrics=self.contract['metrics'])
        column=lineage['column_metadata']['by_channel_orders']
        self.assertEqual(column['measurement_id'],'paid_order_count')
        self.assertTrue(column['requires_review'])

    def test_local_measurement_output_binding_does_not_change_definition_version(self):
        local={'id':'custom_count','definition':'自定义事件计数','scope':'当前对象',
               'source_fields':['payments.order_id'],'output_columns':['n']}
        a=measurement_lineage('SELECT COUNT(*) AS n FROM payments',{'metrics':[]},[],self.graph,local_measurements=[local])
        b=measurement_lineage('SELECT COUNT(*) AS m FROM payments',{'metrics':[]},[],self.graph,local_measurements=[{**local,'output_columns':['m']}])
        self.assertEqual(a['column_metadata']['n']['measurement_id'],'custom_count')
        self.assertEqual(a['column_metadata']['n']['measurement_version'],b['column_metadata']['m']['measurement_version'])

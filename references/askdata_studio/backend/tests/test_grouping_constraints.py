import unittest
from app.querying.grouping_constraints import disconnected_group_metrics


class GroupingConstraintTest(unittest.TestCase):
    graph={'tables':[{'id':'items','name':'items'},{'id':'sessions','name':'sessions'}],
           'fields':[{'table_id':'items','name':'category'},{'table_id':'items','name':'amount'},
                     {'table_id':'sessions','name':'month'},{'table_id':'sessions','name':'sid'}]}
    request={'dimensions':['month','items.category']}
    contract={'metrics':[{'output_columns':['amount']},{'output_columns':['visits']}]}
    sql='''WITH sales AS (SELECT month,category,SUM(amount) amount FROM items GROUP BY month,category),
    visits AS (SELECT month,COUNT(sid) visits FROM sessions GROUP BY month)
    SELECT s.month,s.category,s.amount,v.visits FROM sales s JOIN visits v ON s.month=v.month'''

    def test_broadcast_global_aggregate_is_not_a_group_metric(self):
        errors=disconnected_group_metrics(self.sql,self.request,self.contract,self.graph)
        self.assertEqual(len(errors),1)
        self.assertIn('visits',errors[0])

    def test_global_benchmark_and_multi_origin_share_are_not_rejected(self):
        self.assertEqual(disconnected_group_metrics(self.sql,{**self.request,'comparison_purpose':'整体对照'},self.contract,self.graph),[])
        sql=self.sql.replace('s.amount,v.visits','s.amount/v.visits AS visits')
        self.assertEqual(disconnected_group_metrics(sql,self.request,self.contract,self.graph),[])

    def test_grouped_metric_and_direct_query_are_not_rejected(self):
        self.assertEqual(disconnected_group_metrics(self.sql,self.request,{'metrics':[{'output_columns':['amount']}]},self.graph),[])
        sql='SELECT category,SUM(amount) AS amount FROM items GROUP BY category'
        self.assertEqual(disconnected_group_metrics(sql,self.request,self.contract,self.graph),[])

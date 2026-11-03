import unittest
from app.querying.query_scope import effective_conditions, calendar_months, normalize_conditions


class QueryScopeTest(unittest.TestCase):
    def test_typed_filters_normalize_before_scope_and_query_identity(self):
        query={'conditions':{'filters':[{'field':'category_code','operator':'in','values':['POWER']}]}}
        state={'extraction':{'time_expressions':['2026-07','2026-06']}}
        self.assertEqual(effective_conditions(query,state),
            {'category_code':'POWER','months':['2026-06','2026-07']})
        self.assertEqual(query['conditions']['filters'][0]['values'],['POWER'])
        self.assertEqual(normalize_conditions({'filters':[]},strict=True),{})

    def test_filter_operations_and_unsupported_legacy_conditions_are_preserved(self):
        self.assertEqual(normalize_conditions({'filters':[{'field':'units','operator':'>=','values':[2]}]},strict=True),
            {'units':{'operator':'>=','value':2}})
        self.assertEqual(normalize_conditions({'uncovered_dimension':'value'},strict=True),{'uncovered_dimension':'value'})
        self.assertEqual(normalize_conditions({'events.at':{'gte':'2026-08-01'}},strict=True),
            {'events.at':{'gte':'2026-08-01'}})

    def test_malformed_or_conflicting_conditions_cannot_be_silently_repaired(self):
        malformed={'months':['2026-06'],"months': ['2026-06']":None}
        # Old evidence retains the original bad condition for audit/coverage.
        self.assertEqual(normalize_conditions(malformed),malformed)
        cases=[malformed,{'category_code':None},
            {'filters':[{'field':'category_code','values':[]}]},
            {'filters':[{'field':'category_code','operator':'=','values':['A','B']}]},
            {'category_code':'A','filters':[{'field':'category_code','values':['B']}]},
            {'filters':[{'field':'months','values':['2026-06']}]},
            {'filters':[{'field':'category_code','operator':'LIKE','values':['A%']}]},
            {'filters':[{'field':'category_code','values':[None]}]}]
        for conditions in cases:
            with self.subTest(conditions=conditions),self.assertRaises(ValueError):
                normalize_conditions(conditions,strict=True)

    def test_equivalent_explicit_month_boundaries(self):
        expected=['2026-06','2026-07']
        for period in [{'start':'2026-06-01','end':'2026-07-31','end_inclusive':True},
                       {'start':'2026-05-31T22:00:00Z','end':'2026-07-31T22:00:00Z','end_inclusive':False}]:
            self.assertEqual(calendar_months(period),expected)
            self.assertEqual(effective_conditions({'conditions':{'time_range':period,'category':'A'}},{}),
                             {'months':expected,'category':'A'})

    def test_ambiguous_partial_or_conflicting_ranges_are_not_guessed(self):
        for period in [['2026-06-01','2026-07-31'],{'start':'2026-06-01','end':'2026-07-31'},
                       {'start':'2026-06-02','end':'2026-08-01','end_inclusive':False}]:
            self.assertIsNone(calendar_months(period))
        request={'conditions':{'months':['2026-06'],'time_range':{'start':'2026-08-01','end':'2026-09-01','end_inclusive':False}}}
        self.assertEqual(effective_conditions(request,{}),request['conditions'])

    def test_explicit_physical_timestamp_and_correction_override_initial_months(self):
        state={'extraction':{'time_expressions':['2026-06']},'schema_graph':{
            'fields':[{'name':'at','sql_name':'events.at','data_type':'TIMESTAMP'}]}}
        query={'conditions':{'events.at':{'gte':'2026-08-01'}}}
        self.assertEqual(effective_conditions(query,state),query['conditions'])
        state['confirmed_parameters']={'period':'2026-08'}
        self.assertEqual(effective_conditions({},state),{})

    def test_typed_filter_and_comparison_preserve_explicit_statistical_scope(self):
        state={'extraction':{'time_expressions':['2026-06']},'schema_graph':{
            'fields':[{'name':'at','sql_name':'events.at','data_type':'TIMESTAMP'}]}}
        query={'conditions':{'filters':[{'field':'events.at','operator':'>=','values':['2026-08-01']}]}}
        self.assertEqual(effective_conditions(query,state),
            {'events.at':{'operator':'>=','value':'2026-08-01'}})
        comparison={'comparison_purpose':'比较其他流量集合','conditions':{'filters':[
            {'field':'source_channel','operator':'not in','values':['internal','test']}]}}
        self.assertEqual(effective_conditions(comparison,state),
            {'source_channel':{'operator':'not in','values':['internal','test']}})

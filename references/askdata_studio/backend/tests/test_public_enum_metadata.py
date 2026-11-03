import unittest
from app.querying.public_enum_metadata import enrich_enum_metadata
from app.querying.query_protocol import sql_facing_graph
from app.retrieval.graph import SchemaGraphBuilder


SCHEMA = {'tables': [{'id': 'snap.events', 'name': 'events', 'label': 'Events',
    'description': 'public events', 'database': 'snap', 'fields': [
        {'name': 'kind', 'description': 'see enum table', 'label': 'Kind', 'type': 'text'}]}]}
DOCUMENT = """### 3.1 `events`: entries
| Field | Type | Meaning |
|---|---|---|
| `kind` | TEXT | see below |

| `kind` | Meaning | Delta |
|---|---|---|
| `seed` | initial balance | initial amount |
| `change` | subsequent change | signed amount |

Do not import this surrounding prose.
### 3.2 `unregistered`: unrelated
| `kind` | Meaning |
|---|---|
| `secret` | not authorized |
"""


class PublicEnumMetadataTest(unittest.TestCase):
    def test_registered_enum_values_and_meanings_without_surrounding_prose(self):
        result = enrich_enum_metadata(SCHEMA, DOCUMENT, source='snap/data_dictionary.md')
        field = result['tables'][0]['fields'][0]
        self.assertEqual(field['enum_values'], ['seed', 'change'])
        self.assertEqual(field['enum_descriptions']['seed']['Meaning'], 'initial balance')
        self.assertNotIn('surrounding prose', str(result))
        self.assertNotIn('secret', str(result))
        self.assertNotIn('enum_values', SCHEMA['tables'][0]['fields'][0])

    def test_conflicting_registered_values_are_not_silently_replaced(self):
        import copy
        payload=copy.deepcopy(SCHEMA)
        payload['tables'][0]['fields'][0]['enum_values']=['old']
        field=enrich_enum_metadata(payload,DOCUMENT,source='dictionary')['tables'][0]['fields'][0]
        self.assertEqual(field['enum_values'], ['old'])
        self.assertIn('enum_metadata_conflict',field)
        self.assertNotIn('enum_descriptions',field)

    def test_malformed_or_duplicate_values_do_not_create_a_partial_enum(self):
        for document in [DOCUMENT.replace('`change`','`seed`'),DOCUMENT.replace('| `change` |','| unknown |')]:
            with self.subTest(document=document):
                field=enrich_enum_metadata(SCHEMA,document,source='dictionary')['tables'][0]['fields'][0]
                self.assertNotIn('enum_values',field)

    def test_repeated_enrichment_does_not_duplicate_metadata(self):
        a=enrich_enum_metadata(SCHEMA,DOCUMENT,source='dictionary')
        self.assertEqual(a,enrich_enum_metadata(a,DOCUMENT,source='dictionary'))

    def test_selected_graph_and_sql_context_retain_meaning_from_canonical_metadata(self):
        payload=enrich_enum_metadata(SCHEMA,DOCUMENT,source='dictionary')
        builder=SchemaGraphBuilder();builder.tables={'snap.events':payload['tables'][0]}
        graph=builder.build([{'doc_id':'snap.events.kind','database_id':'snap','table_id':'snap.events',
            'field_name':'kind','field_label':'Kind','field_type':'text','field_description':'see below'}])
        field=sql_facing_graph(graph)['fields'][0]
        self.assertEqual(field['enum_descriptions']['seed']['Meaning'],'initial balance')
        self.assertEqual(field['enum_metadata_source']['source'],'dictionary')

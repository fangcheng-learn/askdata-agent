import unittest
from app.workflows.review_references import context_directory, normalize_finding_references
from app.workflows.investigation_output import output_schema


class ReviewReferenceTest(unittest.TestCase):
    def test_only_supplied_context_sections_are_addressable(self):
        directory=context_directory({'task':{},'data_capabilities':{},'private_notes':'hidden'})
        self.assertEqual(set(directory),{'context:task','context:data_capabilities'})
        self.assertFalse(directory['context:task']['numeric_evidence'])

    def test_exact_section_reference_is_retyped_without_becoming_query_evidence(self):
        original={'audit_findings':[{'criterion':'capability','evidence_ids':['T:R1','data_capabilities']}]}
        directory=context_directory({'data_capabilities':{}})
        result,changes=normalize_finding_references(original,directory,{'T:R1'})
        finding=result['audit_findings'][0]
        self.assertEqual(finding['evidence_ids'],['T:R1'])
        self.assertEqual(finding['context_refs'],['context:data_capabilities'])
        self.assertEqual(len(changes),1)
        self.assertEqual(len(original['audit_findings'][0]['evidence_ids']),2)

    def test_unknown_foreign_and_absent_references_remain_invalid(self):
        original={'audit_findings':[{'evidence_ids':['other:R1','data_capabilites','draft_evidence_gaps']}]}
        result,changes=normalize_finding_references(original,context_directory({'data_capabilities':{}}),{'T:R1'})
        self.assertEqual(result['audit_findings'][0]['evidence_ids'],original['audit_findings'][0]['evidence_ids'])
        self.assertEqual(changes,[])

    def test_malformed_reference_collections_are_not_fabricated(self):
        for finding in [{'evidence_ids':'T:R1'},{'evidence_ids':[],'context_refs':'task'}]:
            result,_=normalize_finding_references({'audit_findings':[finding]}, {},set())
            self.assertEqual(result['audit_findings'][0],finding)

    def test_provider_schema_exposes_separate_actual_reference_directories(self):
        schema=output_schema('delivery_review',result_columns={'T:R1':['n']},review_context_refs=['context:task'])
        finding=schema['$defs']['ReviewFinding']['properties']
        self.assertEqual(finding['evidence_ids']['items']['enum'],['T:R1'])
        self.assertEqual(finding['context_refs']['items']['enum'],['context:task'])
        question=schema['$defs']['QuestionAssessment']['properties']['evidence_ids']
        self.assertEqual(question['items']['enum'],['T:R1'])

    def test_no_evidence_does_not_offer_a_fake_result_id(self):
        schema=output_schema('delivery_review',result_columns={},review_context_refs=[])
        finding=schema['$defs']['ReviewFinding']['properties']
        self.assertEqual(finding['evidence_ids']['maxItems'],0)
        self.assertEqual(finding['context_refs']['maxItems'],0)

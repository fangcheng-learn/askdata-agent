import unittest
from app.business_progress import snapshot_updates,query_table,emit_snapshot,emit_plan
from app.workbench_runtime import ExecutionControl,current_execution
from app.foundry_adapter.projection import artifacts,custom_events

class BusinessProgressTests(unittest.TestCase):
    def task(self):
        return {'state':{'task_id':'t'},'actions':[{'id':'Q1','action_type':'query','goal':'比较六月与七月GMV','status':'running'}],
            'progress':{'facts':[{'id':'F1','text':'七月GMV下降21%','evidence_ids':['t:R1']}]},
            'evidence':[{'id':'t:R1','result_ref':'ref1','goal':'比较六月与七月GMV','columns':['gmv'],'rows':[{'gmv':10}],'total_rows':1,'complete':True,'check_status':'basic_valid'}]}
    def test_updates_are_actual_business_notes_and_stream_once_until_changed(self):
        events=[];control=ExecutionControl(on_event=events.append);token=current_execution.set(control)
        try:
            task=self.task();task['logs']=[{'chain_of_thought':'DO NOT DISPLAY'}]
            emit_plan('t',{'type':'query_batch','queries':[{'goal':'比较六月与七月GMV'}]},1)
            emit_snapshot(task);count=len(events);emit_snapshot(task)
            self.assertEqual(len(events),count)
            task['actions'][0]['status']='completed';emit_snapshot(task)
            self.assertEqual(len(events),count+1)
            self.assertNotIn('DO NOT DISPLAY',str(events))
            self.assertTrue(any(e['value'].get('kind')=='finding' for e in events))
            self.assertTrue(any(e['name']=='artifact' for e in events))
        finally:current_execution.reset(token)
    def test_superseded_evidence_does_not_remain_a_verified_finding(self):
        task=self.task();task['evidence'][0]['check_status']='superseded'
        note=next(e['value'] for e in snapshot_updates(task) if e.get('name')=='insightpilot.analysis' and e['value']['kind']=='finding')
        self.assertEqual(note['title'],'待重新核对的发现')
        self.assertEqual(note['status'],'open')
    def test_rounds_keep_actions_results_and_validated_findings_stable(self):
        task=self.task();task['actions'][0]['business_round']=2;task['evidence'][0]['step_id']='Q1'
        task['business_note_rounds']={'facts:F1':3}
        task['logs']=[{'stage':'react_decision','revision':4,'action':{'type':'deliver'}}]
        updates=snapshot_updates(task)
        notes={e['value']['kind']:e['value'] for e in updates if e['name']=='insightpilot.analysis'}
        self.assertEqual([notes[k]['round'] for k in ('action','result','finding')],[2,2,3])
        task['logs'].append({'stage':'react_decision','revision':5,'action':{'type':'deliver'}})
        self.assertEqual(snapshot_updates(task),updates)
        legacy={**notes['finding'],'id':'t-finding-F1'};legacy.pop('round')
        record={'task_id':'t','result':{},'execution_events':[{'type':'CUSTOM','name':'insightpilot.analysis','value':legacy}],'business_updates':updates}
        findings=[e for e in custom_events(record,'s') if e['name']=='insightpilot.analysis' and e['value']['kind']=='finding']
        self.assertEqual(len(findings),1)

    def test_table_id_survives_validation_and_empty_results_are_visible(self):
        raw={'result_ref':'ref1','columns':['gmv'],'rows':[],'total_rows':0,'complete':True}
        self.assertEqual(query_table(raw,'tool1')['preview_json']['rows'],[])
        self.assertIn('共 0 行',query_table(raw,'tool1')['summary'])
        self.assertEqual(query_table(raw,'tool1')['id'],query_table({**raw,'check_status':'basic_valid'},'R1')['id'])
        self.assertIsNone(query_table({**raw,'success':False},'tool1'))
        limited=query_table({**raw,'rows':[{'gmv':i} for i in range(40)],'total_rows':40},'tool1')
        self.assertEqual(len(limited['preview_json']['rows']),20)
        self.assertTrue(limited['preview_json']['truncated'])
    def test_history_restores_business_updates_and_latest_table_validation(self):
        updates=snapshot_updates(self.task());raw=query_table(self.task()['evidence'][0],'tool1')
        record={'task_id':'t','run_id':'run','result':{},'execution_events':[{'type':'CUSTOM','name':'artifact','value':raw}], 'business_updates':updates}
        tables=artifacts(record);self.assertEqual(len(tables),1)
        self.assertIn('已通过基础校验',tables[0]['summary'])
        events=custom_events(record,'session')
        self.assertTrue(any(e['name']=='insightpilot.analysis' and e['value']['kind']=='finding' for e in events))
        self.assertTrue(all(e['runId']=='run' for e in events))

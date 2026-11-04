// @vitest-environment happy-dom
import {describe,it,expect} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {createElement,act} from 'react';
import {createRoot} from 'react-dom/client';
import {createInitialLiveRun,reduceLiveRunEvent} from '../live-run-state';
import {hydrateLiveRunFromConversation} from '../conversation-restore';
import {BusinessProgress,RunningBusinessProgress,LiveQueryTables,businessEntriesForTurn,groupBusinessRounds,businessStageStatus,effectiveBusinessNoteStatus} from '../components/BusinessProgress';
import type {BusinessProgressEntry} from '../live-run-state';
import type {SessionConversationDto} from '../../../../lib/config-api/types';
const note={id:'t-F1',kind:'finding',title:'已核实的发现',body:'七月GMV下降21%',status:'supported',evidenceIds:['t:R1']};
it('replaces the live cursor before any answer and retires when delivery finishes',()=>{
  let run=reduceLiveRunEvent(createInitialLiveRun(),{type:'RUN_STARTED',runId:'new-run'});
  const html=()=>renderToStaticMarkup(createElement(RunningBusinessProgress,{liveRun:run}));
  expect(html()).toContain('正在准备分析');
  expect(html()).toContain('running-business-progress');
  run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,title:'正在查询各渠道GMV'}});
  expect(html()).toContain('正在查询各渠道GMV');
  run=reduceLiveRunEvent(run,{type:'RUN_FINISHED',runId:'new-run'});
  expect(html()).toBe('');
});
const table={id:'query-ref1',title:'月度GMV',type:'table',summary:'待核对 · 共 1 行',preview_json:{columns:['gmv'],rows:[{gmv:10}]}};
it('keeps failed retries in one business stage and resolves only with actual success',()=>{
 const entry=(round:number,kind:string,status:string,id:string,body='核实同一渠道两期客单价'):BusinessProgressEntry=>({id,runId:'r',round,kind,status,body,title:'查证',evidenceIds:[]});
 const entries=[entry(7,'action','needs_method_decision','t-action-A5-round-7'),entry(7,'issue','open','t-issue-A5-round-7','缺少期间'),
  entry(8,'plan','planned','t-plan-8'),entry(8,'action','needs_method_decision','t-action-A6-round-8'),
  entry(8,'issue','open','t-issue-A6-round-8','缺少期间'),entry(9,'plan','planned','t-plan-9'),entry(10,'plan','planned','t-plan-10')];
 let stages=groupBusinessRounds(entries);
 expect(stages).toHaveLength(1);expect(stages[0].roundNumbers).toEqual([7,8,9,10]);
 expect(businessStageStatus(stages[0].notes,true)).toBe('正在处理');
 expect(businessStageStatus(stages[0].notes,false)).toBe('未解决');
 entries.push(entry(10,'action','completed','t-action-A7-round-10'));
 stages=groupBusinessRounds(entries);
 expect(stages).toHaveLength(1);expect(businessStageStatus(stages[0].notes,false)).toBe('处理完成');
 expect(effectiveBusinessNoteStatus(entries[1],stages[0].notes)).toBe('resolved');
 expect(entries[1].status).toBe('open'); // historical event remains unchanged
 entries.push(entry(10,'issue','open','t-question-Q1-round-10','另一个尚未核实的业务问题'));
 expect(businessStageStatus(groupBusinessRounds(entries)[0].notes,false)).toBe('未解决');
});
it('does not merge different goals, completed investigations or other runs',()=>{
 const make=(round:number,body:string,status:string,runId='r'):BusinessProgressEntry=>({id:`${runId}-${round}`,round,body,status,runId,kind:'action',title:'查证',evidenceIds:[]});
 expect(groupBusinessRounds([make(1,'六月范围','failed'),make(2,'七月范围','completed'),make(3,'七月范围','running'),make(1,'六月范围','running','other')])).toHaveLength(4);
});
describe('business progress and live result tables',()=>{
 it('shows a table while analysis is still running and updates validation without duplicates',()=>{
   let run=reduceLiveRunEvent(createInitialLiveRun(),{type:'RUN_STARTED',runId:'run'});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'artifact',value:table});
   expect(run.runStatus).toBe('running');expect(run.artifacts[0].detail).toEqual({type:'dataset',columns:['gmv'],rows:[['10']]});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'artifact',value:{...table,summary:'已通过基础校验 · 共 1 行'}});
   expect(run.artifacts).toHaveLength(1);
   const html=renderToStaticMarkup(createElement(LiveQueryTables,{artifacts:run.artifacts,onOpen:()=>{}}));
   expect(html).toContain('查询结果表');expect(html).toContain('已通过基础校验');expect(html).toContain('10');
 });
 it('upserts key progress and scopes it to the right conversation turn',()=>{
   let run=reduceLiveRunEvent(createInitialLiveRun(),{type:'RUN_STARTED',runId:'run'});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:note});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,status:'refuted'}});
   expect(run.businessProgress).toHaveLength(1);
   expect(businessEntriesForTurn(run,'run-user',false)).toHaveLength(1);
   expect(businessEntriesForTurn(run,'old-user',false)).toHaveLength(0);
   const html=renderToStaticMarkup(createElement(BusinessProgress,{liveRun:run,current:true}));
   expect(html).toContain('七月GMV下降21%');expect(html).toContain('收起过程');expect(html).not.toContain('query_homenova');
 });
 it('opens the process by default and preserves user choices through streaming and completion',async()=>{
   Object.assign(globalThis,{IS_REACT_ACT_ENVIRONMENT:true});
   let run=reduceLiveRunEvent(createInitialLiveRun(),{type:'RUN_STARTED',runId:'run'});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,round:1}});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,id:'t-F2',round:2,body:'转化率下降'}});
   const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
   const render=()=>act(async()=>root.render(createElement(BusinessProgress,{liveRun:run,current:true})));
   await render();
   const buttons=()=>Array.from(host.querySelectorAll('button'));
   expect(buttons().map(b=>b.getAttribute('aria-expanded'))).toEqual(['true','false','false']);
   await act(async()=>buttons()[1].click());
   await act(async()=>buttons()[0].click());
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,id:'t-F3',round:3,body:'检查库存'}});
   await render();expect(buttons()).toHaveLength(1);expect(buttons()[0].getAttribute('aria-expanded')).toBe('false');
   await act(async()=>buttons()[0].click());
   expect(buttons().map(b=>b.getAttribute('aria-expanded'))).toEqual(['true','true','false','false']);
   await act(async()=>buttons()[0].click());
   run={...run,runStatus:'completed'};await render();
   expect(buttons()).toHaveLength(1);expect(buttons()[0].getAttribute('aria-expanded')).toBe('false');
   await act(async()=>buttons()[0].click());
   expect(buttons().map(b=>b.getAttribute('aria-expanded'))).toEqual(['true','true','false','false']);
   run={...run,runId:'other-run',businessProgress:run.businessProgress.map(entry=>({...entry,runId:'other-run'}))};await render();
   expect(buttons().map(b=>b.getAttribute('aria-expanded'))).toEqual(['true','false','false','false']);
   await act(async()=>root.unmount());host.remove();
 });
 it('restores meaningful notes and tables without starting another run',()=>{
   const dto={sessionId:'s',messages:[{id:'run-user',messageId:'run-user',runId:'run',role:'user',source:'client',contentText:'为什么下降',position:0}],toolCalls:[],checkpoints:[{runId:'run',status:'completed',messageStartPosition:0,messageEndPosition:0}],restorableCustomEvents:[{runId:'run',seq:1,name:'insightpilot.analysis',value:note},{runId:'run',seq:2,name:'artifact',value:table}],runEventRefs:[],branches:[],activeRun:null} as SessionConversationDto;
   const run=hydrateLiveRunFromConversation(createInitialLiveRun(),dto);
   expect(run.runStatus).toBe('completed');expect(run.businessProgress?.[0].body).toBe(note.body);expect(run.artifacts).toHaveLength(1);
 });
});

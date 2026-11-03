// @vitest-environment happy-dom
import {describe,it,expect} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {createElement,act} from 'react';
import {createRoot} from 'react-dom/client';
import {createInitialLiveRun,reduceLiveRunEvent} from '../live-run-state';
import {hydrateLiveRunFromConversation} from '../conversation-restore';
import {BusinessProgress,RunningBusinessProgress,LiveQueryTables,businessEntriesForTurn} from '../components/BusinessProgress';
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
   expect(html).not.toContain('七月GMV下降21%');expect(html).toContain('查看过程');expect(html).not.toContain('query_homenova');
 });
 it('keeps one collapsed status, expands stages on demand, and respects streaming updates',async()=>{
   Object.assign(globalThis,{IS_REACT_ACT_ENVIRONMENT:true});
   let run=reduceLiveRunEvent(createInitialLiveRun(),{type:'RUN_STARTED',runId:'run'});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,round:1}});
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,id:'t-F2',round:2,body:'转化率下降'}});
   const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
   const render=()=>act(async()=>root.render(createElement(BusinessProgress,{liveRun:run,current:true})));
   await render();
   const buttons=()=>Array.from(host.querySelectorAll('button'));
   expect(buttons().map(b=>b.getAttribute('aria-expanded'))).toEqual(['false']);
   await act(async()=>buttons()[0].click());
   expect(buttons().map(b=>b.getAttribute('aria-expanded'))).toEqual(['true','false','false']);
   await act(async()=>buttons()[1].click());
   await act(async()=>buttons()[0].click());
   run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{...note,id:'t-F3',round:3,body:'检查库存'}});
   await render();expect(buttons()).toHaveLength(1);expect(buttons()[0].getAttribute('aria-expanded')).toBe('false');
   await act(async()=>buttons()[0].click());
   expect(buttons().map(b=>b.getAttribute('aria-expanded'))).toEqual(['true','true','false','false']);
   run={...run,runStatus:'completed'};await render();
   expect(buttons()).toHaveLength(1);expect(buttons()[0].getAttribute('aria-expanded')).toBe('false');
   await act(async()=>root.unmount());host.remove();
 });
 it('restores meaningful notes and tables without starting another run',()=>{
   const dto={sessionId:'s',messages:[{id:'run-user',messageId:'run-user',runId:'run',role:'user',source:'client',contentText:'为什么下降',position:0}],toolCalls:[],checkpoints:[{runId:'run',status:'completed',messageStartPosition:0,messageEndPosition:0}],restorableCustomEvents:[{runId:'run',seq:1,name:'insightpilot.analysis',value:note},{runId:'run',seq:2,name:'artifact',value:table}],runEventRefs:[],branches:[],activeRun:null} as SessionConversationDto;
   const run=hydrateLiveRunFromConversation(createInitialLiveRun(),dto);
   expect(run.runStatus).toBe('completed');expect(run.businessProgress?.[0].body).toBe(note.body);expect(run.artifacts).toHaveLength(1);
 });
});

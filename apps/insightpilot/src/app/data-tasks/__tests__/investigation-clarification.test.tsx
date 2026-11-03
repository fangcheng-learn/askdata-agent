// @vitest-environment happy-dom
import {it,expect,vi} from 'vitest';
vi.mock('@copilotkit/react-core/v2',()=>({useAgent:vi.fn(),useCopilotChatConfiguration:vi.fn(),useCopilotKit:vi.fn()}));
import {createElement,act} from 'react';
import {createRoot} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {InvestigationQuestionCard} from '../components/chat/InvestigationClarification';
import {createInitialLiveRun,reduceLiveRunEvent} from '../live-run-state';
import {BusinessProgress} from '../components/BusinessProgress';
it('required clarification has no skip and cannot submit an empty answer',()=>{
 const html=renderToStaticMarkup(createElement(InvestigationQuestionCard,{question:{question:'请指定期间',reason:'期间影响比较',required:true,allow_skip:true},onSubmit:async()=>{}}));
 expect(html).not.toContain('跳过，继续分析');expect(html).toContain('disabled');expect(html).toContain('请指定期间');
});
it('optional skip sends the explicit skip intent and does not select an answer',async()=>{
 Object.assign(globalThis,{IS_REACT_ACT_ENVIRONMENT:true});
 const onSubmit=vi.fn(async()=>{});const host=document.createElement('div');const root=createRoot(host);
 await act(async()=>root.render(createElement(InvestigationQuestionCard,{question:{question:'七月有活动吗',reason:'核实业务背景',required:false,allow_skip:true,options:[{id:'yes',label:'有'},{id:'no',label:'没有'}]},onSubmit})));
 expect(host.querySelector('input:checked')).toBeNull();
 await act(async()=>Array.from(host.querySelectorAll('button')).find(b=>b.textContent==='跳过，继续分析')!.click());
 expect(onSubmit).toHaveBeenCalledExactlyOnceWith('跳过，继续分析');await act(async()=>root.unmount());
});
it('delivery preserves question and no-report status survives the completed transport',()=>{
 let run=reduceLiveRunEvent(createInitialLiveRun(),{type:'RUN_STARTED',runId:'run'});
 run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.delivery',value:{runId:'run',taskId:'task',status:'waiting_clarification',hasReport:false,clarification:{question:'请指定期间',reason:'缺少期间',required:true,allow_skip:false,options:[]}}});
 expect(run.delivery?.clarification?.required).toBe(true);expect(run.delivery?.taskId).toBe('task');
 run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.analysis',value:{id:'issue',kind:'issue',body:'查询未完成',status:'open'}});
 run=reduceLiveRunEvent(run,{type:'CUSTOM',name:'insightpilot.delivery',value:{runId:'run',status:'partial',hasReport:false}});
 run=reduceLiveRunEvent(run,{type:'RUN_FINISHED',runId:'run'});
 const html=renderToStaticMarkup(createElement(BusinessProgress,{liveRun:run,current:true}));
 expect(html).toContain('报告未交付');expect(html).not.toContain('已交付当前分析');
});

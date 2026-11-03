import {describe,it,expect} from 'vitest';
import {createInitialLiveRun,reduceLiveRunEvent} from '../live-run-state';
import {buildProcessToolGroups,deriveProcessGroupUsage} from '../process-tool-groups';
import {toolDisplayTitle} from '../data-task-state';
import {toolResultLooksLikeError} from '../tool-call-display';

describe('InsightPilot live execution protocol',()=>{
  it('shows a real model stage before any tool completes, without inventing tool calls',()=>{
    let state=reduceLiveRunEvent(createInitialLiveRun(),{type:'RUN_STARTED',runId:'a'});
    state=reduceLiveRunEvent(state,{type:'ACTIVITY_SNAPSHOT',messageId:'phase',activityType:'STEP',timestamp:100,
      content:{step_id:'phase',title:'正在理解问题',status:'running',phase:true}});
    const groups=buildProcessToolGroups([],state);
    expect(groups).toHaveLength(1);expect(groups[0].title).toBe('正在理解问题');
    expect(groups[0].status).toBe('running');expect(deriveProcessGroupUsage(groups,state).toolCallCount).toBe(0);
    state=reduceLiveRunEvent(state,{type:'STATE_SNAPSHOT',snapshot:{runId:'a',runStatus:'canceled'}});
    state=reduceLiveRunEvent(state,{type:'RUN_FINISHED',runId:'a'});
    expect(state.runStatus).toBe('canceled');expect(buildProcessToolGroups([],state)[0].status).not.toBe('running');
  });
  it('restores phase snapshots and keeps separate session state unchanged',()=>{
    const oldSession=createInitialLiveRun();
    const phase={type:'ACTIVITY_SNAPSHOT',messageId:'phase',activityType:'STEP',timestamp:100,
      content:{step_id:'phase',title:'正在核对结论与证据',status:'completed',phase:true}};
    const restored=reduceLiveRunEvent(createInitialLiveRun(),{type:'CUSTOM',name:'insightpilot.activity',value:phase});
    expect(buildProcessToolGroups([],restored)[0].title).toBe('正在核对结论与证据');
    expect(oldSession.runStatus).toBe('idle');expect(oldSession.events).toHaveLength(0);
  });
  it('does not classify successful nullable error results as failures',()=>{
    const content=JSON.stringify({success:true,error:null,rows:[{gmv:10}],row_count:1,sql:'SELECT 10 AS gmv'});
    expect(toolResultLooksLikeError(content)).toBe(false);
    let state=reduceLiveRunEvent(createInitialLiveRun(),{type:'TOOL_CALL_START',toolCallId:'sql',toolCallName:'query_homenova'});
    state=reduceLiveRunEvent(state,{type:'TOOL_CALL_END',toolCallId:'sql'});
    state=reduceLiveRunEvent(state,{type:'TOOL_CALL_RESULT',toolCallId:'sql',content});
    expect(state.toolCalls[0].status).toBe('success');
    expect(state.events[0].kind).toBe('query');
    expect(toolResultLooksLikeError(JSON.stringify({success:false,error:'query failed'}))).toBe(true);
  });
  it('uses Chinese business tool names without changing the query identifiers',()=>{
    expect(toolDisplayTitle('query_homenova_2026_08_31')).toBe('查询经营数据');
    expect(toolDisplayTitle('current_datetime')).toBe('获取当前时间');
  });
});

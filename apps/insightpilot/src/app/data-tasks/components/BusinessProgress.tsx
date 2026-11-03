"use client";
import React, {useEffect,useState} from 'react';
import type {LiveRun, BusinessProgressEntry} from '../live-run-state';
import {getRunDisplayStatus} from '../live-run-state';
import type {DataArtifact} from '../data-task-state';

const statuses: Record<string,string> = {
  planned:'准备中',running:'进行中',generating:'准备中',rewriting:'正在调整',completed:'已完成',failed:'受阻',blocked:'受阻',
  supported:'有证据支持',refuted:'已排除',open:'待验证',unresolved:'待验证',weak:'证据不足',
  low_explanatory_power:'解释力有限',unsupported:'缺少支持',basic_valid:'已核对',reviewed:'已审核',
  superseded:'已被替代',resolved:'已解决',interrupted:'已中止',no_progress:'尚无新增结果',
};
export function businessEntriesForTurn(run:LiveRun,messageId:string,isLatest:boolean) {
  return (run.businessProgress ?? []).filter(e=>messageId.startsWith(e.runId+'-') || (isLatest && e.runId===run.runId));
}

export function RunningBusinessProgress({liveRun}: {liveRun:LiveRun | null}) {
  if (!liveRun || liveRun.runStatus !== 'running') return null;
  return <div data-testid="running-business-progress" className="min-w-0 px-1 pb-3">
    <BusinessProgress liveRun={liveRun} current />
  </div>;
}

export function groupBusinessRounds(entries:BusinessProgressEntry[]) {
  const rounds=new Map<number,BusinessProgressEntry[]>();
  for(const entry of entries) {
    const number=Number.isFinite(entry.round) && entry.round>0?entry.round:1;
    const group=rounds.get(number) ?? [];
    group.push(entry);rounds.set(number,group);
  }
  return [...rounds].sort(([a],[b])=>a-b).map(([number,notes])=>({number,notes:notes.filter(n=>n.kind!=='plan' || !notes.some(a=>a.kind==='action' && a.body===n.body))}));
}

export function BusinessProgress({liveRun, entries, current=false, defaultExpanded=false,onOpenTrace}: {liveRun:LiveRun;entries?:BusinessProgressEntry[];current?:boolean;defaultExpanded?:boolean;onOpenTrace?:()=>void}) {
  const [expanded,setExpanded]=useState<boolean>();
  const [roundExpansion,setRoundExpansion]=useState<Record<number,boolean>>({});
  const all=entries ?? (liveRun.businessProgress ?? []).filter(e=>e.runId===liveRun.runId);
  const running=current && liveRun.runStatus==='running';
  const rounds=groupBusinessRounds(all);
  const open=expanded ?? defaultExpanded;
  const stage=[...liveRun.events].reverse().find(e=>e.activityStatus==='running');
  const latest=all.at(-1);
  const label=!current ? '工作过程' : running ? (stage?.title || latest?.title || '正在准备分析')
    : getRunDisplayStatus(liveRun)==='suspended' ? '等待你的回复'
    : liveRun.delivery?.runId===liveRun.runId && liveRun.delivery?.status==='partial' ? (liveRun.delivery.hasReport ? '已交付当前分析' : '调查已结束，报告未交付')
    : liveRun.runStatus==='failed' ? '本次运行受阻'
    : liveRun.runStatus==='canceled' ? '已停止' : '工作过程';
  useEffect(()=>{setExpanded(undefined);setRoundExpansion({});},[liveRun.runId,liveRun.runStatus]);
  if(!all.length && !running && !(current && getRunDisplayStatus(liveRun)==='suspended'))return null;
  return <section aria-label="分析关键进展" className="min-w-0 text-sm">
    <button type="button" aria-expanded={open} onClick={()=>setExpanded(!open)} className="flex min-h-12 w-full cursor-pointer items-center gap-2 rounded-md px-1 text-left text-xs text-muted transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
      <span aria-hidden="true" className={running?'motion-safe:animate-pulse':''}>{running?'●':open?'⌄':'›'}</span>
      <span role="status" aria-live="polite" className="min-w-0 flex-1 truncate">{label}</span>
      <span className="shrink-0 text-muted-light">{rounds.length>0?`${rounds.length} 个阶段`:''}</span>
      <span className="shrink-0">{open?'收起过程':'查看过程'}</span>
    </button>
    {open && <div className="max-h-[40dvh] overflow-y-auto overscroll-contain pb-3"><ol className="divide-y divide-border border-t border-border">
      {rounds.map((round,index)=>{
        const active=running && index===rounds.length-1;
        const roundOpen=roundExpansion[round.number] ?? false;
        const headline=round.notes.find(n=>n.kind==='plan') ?? round.notes.find(n=>n.kind==='action') ?? round.notes[0];
        const findings=round.notes.filter(n=>n.kind==='finding' || n.kind==='explanation').length;
        const results=round.notes.filter(n=>n.kind==='result').length;
        const issues=round.notes.some(n=>n.kind==='issue' && n.status!=='resolved');
        return <li key={round.number} className="min-w-0 py-1">
          <button type="button" aria-expanded={roundOpen} onClick={()=>setRoundExpansion(v=>({...v,[round.number]:!roundOpen}))} className="flex w-full min-w-0 cursor-pointer items-start gap-2 py-3 text-left hover:text-muted">
            <span aria-hidden="true" className="mt-0.5 text-muted-light">{roundOpen?'⌄':'›'}</span>
            <span className="min-w-0 flex-1"><span className="flex items-center gap-2 text-xs"><span className="font-medium">阶段 {index+1}</span><span className={issues?'text-step-warning':'text-muted-light'}>{active?'进行中':issues?'有待解决的问题':'阶段记录'}</span></span>
              <span className="mt-1 block truncate leading-6" title={headline.body}>{headline.body}</span>
              <span className="mt-1 block text-[11px] text-muted-light">{[results?`${results} 项数据结果`:'',findings?`${findings} 项分析发现`:'',`${round.notes.length} 条进展`].filter(Boolean).join(' · ')}</span>
            </span>
          </button>
          {roundOpen && <ol className="mb-3 ml-2 grid gap-4 border-l border-border pl-4">
            {round.notes.map(note=><li key={note.id} className="min-w-0"><details>
              <summary className="cursor-pointer text-xs leading-6 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"><span className={note.kind==='issue'?'text-step-warning':'text-muted'}>{note.kind==='plan'?'本轮安排':note.title}</span><span className="ml-2 text-muted-light">{!running && ['running','generating','rewriting'].includes(note.status)?'未完成':statuses[note.status] ?? ''}</span></summary>
              <p className="mt-2 whitespace-pre-wrap break-words leading-6 text-foreground">{note.body}</p>
              {note.evidenceIds.length>0 && <div className="mt-1 text-[11px] text-muted-light">依据：{note.evidenceIds.map(id=>id.split(':').at(-1)).join('、')}</div>}
            </details></li>)}
          </ol>}
        </li>;
      })}
      {!rounds.length && <li className="py-3 text-xs text-muted-light">正在准备本轮分析…</li>}
    </ol>{onOpenTrace && <button type="button" onClick={onOpenTrace} className="mt-3 cursor-pointer text-xs text-muted underline underline-offset-4 hover:text-foreground">查看完整运行记录</button>}</div>}
  </section>;
}

export function LiveQueryTables({artifacts,onOpen}: {artifacts:DataArtifact[];onOpen:(id:string)=>void}) {
  const tables=artifacts.filter(a=>a.detail?.type==='dataset');
  const [selected,setSelected]=useState<string>();
  const table=tables.find(t=>t.id===selected) ?? tables.at(-1);
  if(!table || table.detail?.type!=='dataset')return null;
  const data=table.detail;
  return <section aria-label="查询结果表" className="min-w-0 rounded-xl border border-border bg-surface p-3">
    <div className="mb-3 flex items-center justify-between"><h3 className="text-sm font-semibold">查询结果表 <span className="text-xs font-normal text-muted-light">{tables.length}</span></h3><button className="cursor-pointer text-xs text-muted hover:underline" onClick={()=>onOpen(table.id)}>展开查看</button></div>
    <div className="mb-2 flex gap-1 overflow-x-auto" aria-label="选择查询结果表">{tables.map((t,i)=><button key={t.id} title={t.title} onClick={()=>setSelected(t.id)} aria-pressed={t.id===table.id} className={`max-w-48 shrink-0 cursor-pointer truncate rounded-md px-2 py-1 text-xs ${t.id===table.id?'bg-foreground text-surface':'bg-surface-subtle text-muted'}`}>{i+1}. {t.title}</button>)}</div>
    <p className="mb-1 break-words text-xs font-medium">{table.title}</p><p className="mb-2 text-[11px] text-muted-light">{table.summary}</p>
    <div className="max-h-64 overflow-auto rounded-lg border border-border"><table className="w-full text-left text-xs"><thead className="sticky top-0 bg-surface-subtle"><tr>{data.columns.map((c,i)=><th key={i} className="whitespace-nowrap border-b border-border px-2 py-2 font-medium">{c}</th>)}</tr></thead><tbody>{data.rows.map((row,i)=><tr key={i}>{data.columns.map((_,j)=><td key={j} className="max-w-72 whitespace-nowrap border-b border-border px-2 py-1.5">{row[j] ?? '—'}</td>)}</tr>)}</tbody></table>{!data.rows.length && <p className="p-3 text-xs text-muted">查询完成，没有符合条件的数据。</p>}</div>
  </section>;
}

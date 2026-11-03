"use client";

import React, {useState} from "react";
import {useAgent, useCopilotChatConfiguration, useCopilotKit} from "@copilotkit/react-core/v2";
import type {LiveRun} from "../../live-run-state";
import {buildAgentRunStatePatch} from "../../data-task-state";
import {useDataTaskChatInputBindings} from "./DataTaskChatInputBindingsContext";

export type InvestigationQuestion = {
  question: string; reason: string; required?: boolean; allow_skip?: boolean;
  options?: {id: string; label: string; description?: string}[];
};

export function InvestigationQuestionCard({question, onSubmit}: {
  question: InvestigationQuestion; onSubmit: (answer: string) => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const skip = question.allow_skip === true && question.required === false;
  async function submit(value: string) {
    if (busy || !value.trim()) return;
    setBusy(true); setError("");
    try { await onSubmit(value.trim()); }
    catch (e) { setError(e instanceof Error ? e.message : "提交失败，请重试"); setBusy(false); }
  }
  return <section aria-label="补充分析信息" className="my-4 rounded-xl border border-border bg-surface p-4">
    <p className="text-sm font-semibold">{question.question}</p>
    <p className="mt-2 text-xs leading-6 text-muted">{question.reason}</p>
    {question.options?.length ? <div className="mt-3 grid gap-2">
      {question.options.map(option => <label key={option.id} className="flex cursor-pointer items-start gap-2 rounded-lg border border-border p-3 text-sm">
        <input type="radio" name="investigation-answer" checked={answer === option.label} disabled={busy}
          onChange={() => setAnswer(option.label)} />
        <span>{option.label}{option.description && <span className="mt-1 block text-xs text-muted">{option.description}</span>}</span>
      </label>)}
    </div> : <textarea aria-label="你的回答" value={answer} onChange={e => setAnswer(e.target.value)} disabled={busy}
      className="mt-3 min-h-24 w-full rounded-lg border border-border bg-surface p-3 text-sm" placeholder="填写你知道的信息" />}
    <p className="mt-2 text-xs text-muted">{skip ? "跳过后保留这项不确定性，继续分析已有数据。" : "提交必要信息后才会开始或继续调查。"}</p>
    <div className="mt-3 flex flex-wrap gap-2">
      <button type="button" disabled={busy || !answer.trim()} onClick={() => void submit(answer)}
        className="rounded-lg bg-foreground px-4 py-2 text-sm text-surface disabled:opacity-40">{busy ? "正在提交…" : "提交回答"}</button>
      {skip && <button type="button" disabled={busy} onClick={() => void submit("跳过，继续分析")}
        className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-40">跳过，继续分析</button>}
    </div>
    {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
  </section>;
}

export function InvestigationClarification({run}: {run: LiveRun}) {
  const config = useCopilotChatConfiguration();
  const bindings = useDataTaskChatInputBindings();
  const {agent} = useAgent({agentId: config?.agentId ?? bindings.agentId});
  const {copilotkit} = useCopilotKit();
  const delivery = run.delivery;
  if (run.runStatus === "running" || !delivery || delivery.runId !== run.runId || delivery.status !== "waiting_clarification" || !delivery.clarification) return null;
  return <InvestigationQuestionCard key={`${delivery.taskId}:${delivery.clarification.question}`} question={delivery.clarification}
    onSubmit={async text => {
      if (agent.isRunning) throw new Error("当前请求仍在处理，请稍后提交");
      const forwardedProps = bindings.getRunForwardedProps();
      bindings.onUserMessageSubmitted(text);
      agent.setState(buildAgentRunStatePatch(forwardedProps, agent.state));
      agent.addMessage({id: crypto.randomUUID(), role: "user", content: text});
      await copilotkit.runAgent({agent, forwardedProps});
    }} />;
}

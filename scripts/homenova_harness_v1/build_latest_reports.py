"""Offline reports for the two capped trials; never invokes the Agent or a model."""
from __future__ import annotations

import json
from pathlib import Path

from build_process_reports import CSS, code, details, esc, md_report, render_body

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / 'data/homenova_harness_v1/private/implementation_v2'


def build() -> None:
    output = DIRECTORY / 'process_reports'
    for name in ['tasks', 'markdown', 'sources']:
        (output / name).mkdir(parents=True, exist_ok=True)
    (output / 'report.css').write_text(CSS)
    reviews_file = DIRECTORY / 'independent_review.json'
    reviews = json.loads(reviews_file.read_text()) if reviews_file.exists() else {}
    summaries = []
    for case in [1, 2]:
        source = DIRECTORY / f'case_{case}.json'
        if not source.exists():
            continue
        payload = json.loads(source.read_text())
        response = payload['response']
        task_id = response['task_id']
        snapshot = {'source': source, 'payload': payload}
        review = reviews.get(str(case), {})
        task = dict(id=task_id, chosen=snapshot, snapshots=[snapshot], group_label='最新方案实施 · 最多两次真实试运行',
                    task_name=payload['request']['query'], business=review.get('verdict', '未独立核验'),
                    business_note=review.get('reason', '系统状态不代表业务通过。'))
        extra = details('任务要求、前置建议及Controller记录', code({
            'requirements': response.get('requirements'), 'step_decisions': response.get('step_decisions'),
            'controller_events': [event for event in response.get('execution_log', [])
                                  if event.get('stage') in {'controller_decision', 'delivery_return_to_work'}]}), True)
        extra += details('Skill选择、实际注入及记录边界', code({
            'runtime_events': (response.get('budget') or {}).get('events') or '此版本尚未保存Skill逐节点事件',
            'historical_reconstruction': review.get('skill_trace', []),
            'note': 'Skill为应用规则注入，不是额外模型请求。仅记录可观察的执行产物；历史信息缺失不补造。'}), True)
        body = render_body(task).replace('<section><h2>6.', '<section>' + extra + '</section><section><h2>6.', 1)
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        page += '<title>' + esc(task['task_name']) + '</title><link rel="stylesheet" href="../report.css"><body>' + body + '</body></html>'
        (output / 'tasks' / f'{task_id}.html').write_text(page)
        (output / 'markdown' / f'{task_id}.md').write_text(md_report(task) + '\n## 前置建议、任务要求与Skill记录\n\n```json\n' + json.dumps({
            'step_decisions': response.get('step_decisions'), 'requirements': response.get('requirements'),
            'runtime_events': (response.get('budget') or {}).get('events'),
            'historical_skill_reconstruction': review.get('skill_trace', [])}, ensure_ascii=False, indent=2) + '\n```\n')
        (output / 'sources' / f'{task_id}_1.json').write_bytes(source.read_bytes())
        usage = response.get('budget', {}).get('model_usage', [])
        by_kind = {}
        for record in usage:
            group = by_kind.setdefault(record['kind'], {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0,
                                                       'total_tokens': 0, 'cached_tokens': 0})
            group['calls'] += 1
            values = record.get('usage') or {}
            for key in ['prompt_tokens', 'completion_tokens', 'total_tokens']:
                group[key] += values.get(key, 0)
            group['cached_tokens'] += (values.get('prompt_tokens_details') or {}).get('cached_tokens', 0)
        summaries.append(dict(case=case, task_id=task_id, query=payload['request']['query'], status=response['status'],
                              independent_verdict=task['business'], elapsed_seconds=payload.get('elapsed_seconds'),
                              process_reviews=sum(not item.get('final') for item in response.get('reviews', [])),
                              final_reviews=sum(bool(item.get('final')) for item in response.get('reviews', [])),
                              usage=by_kind, budget=response.get('budget'), stop_reason=response.get('stop_reason')))
    (DIRECTORY / 'run_summary.json').write_text(json.dumps({'real_trials': len(summaries), 'limit': 2, 'runs': summaries}, ensure_ascii=False, indent=2) + '\n')
    links = ''.join('<article><h2>第' + str(item['case']) + '题：' + esc(item['query']) + '</h2><p>任务ID：' + esc(item['task_id'])
                    + '；系统状态：' + esc(item['status']) + '；独立判定：' + esc(item['independent_verdict'])
                    + '</p><a href="tasks/' + item['task_id'] + '.html">打开完整过程报告</a></article>' for item in summaries)
    index = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>最新方案两次真实运行</title><link rel="stylesheet" href="report.css"><body><header><h1>最新方案实施：逐任务过程报告</h1><p>按真实保存记录离线生成；最多两次不同问题，无模型或网络调用。旧版历史报告保留。</p></header><section>' + links + '</section></body></html>'
    (output / 'index.html').write_text(index)
    print(json.dumps({'reported_trials': len(summaries), 'network_or_model_calls': False, 'index': str(output / 'index.html')}, ensure_ascii=False))


if __name__ == '__main__':
    build()

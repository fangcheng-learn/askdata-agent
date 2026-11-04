// @vitest-environment happy-dom
import {it,expect,vi} from 'vitest';
import {Streamdown} from 'streamdown';
vi.mock('@copilotkit/react-core/v2',()=>({CopilotChatAssistantMessage:{MarkdownRenderer:({content,...props}:any)=><Streamdown {...props}>{content}</Streamdown>}}));
import React,{act} from 'react';
import {createRoot} from 'react-dom/client';

import {RichMarkdown} from '../components/task-console/ArtifactMarkdownPreview';

it('colors change columns with the actual markdown renderer, leaving absolute values neutral',async()=>{
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true);
  const container=document.createElement('div'); const root=createRoot(container);
  await act(async()=>{root.render(<RichMarkdown content={`## 分析综述

余额下降20元，仍需核实原因。

### 1. 余额变动

| 项目 | 上期余额 | 本期余额 | 增减量 |
| --- | --- | --- | --- |
| A | -100 | -120 | -20 |
| B | 100 | 120 | +20 |

## 行动建议

- 🔴 **高优先级：核对记录**

  余额变动。 确认来源。

- 🟠 **中优先级：核对时段**

  确认变化是否集中。`}/>);});
  const html=container.innerHTML;
  expect(container.querySelectorAll('table')).toHaveLength(1);
  expect(container.querySelectorAll('li')).toHaveLength(2);
  expect(container.querySelectorAll('li')[0].textContent).toContain('🔴 高优先级：核对记录');
  expect(container.querySelectorAll('li')[0].textContent).toContain('确认来源');
  await act(async()=>root.unmount());
  expect(html).toContain('data-change="down"');
  expect(html).toContain('data-change="up"');
  expect(html).not.toMatch(/data-metric-annotation[^>]*>-100</);
  expect(html).not.toMatch(/data-metric-annotation[^>]*>-120</);
  expect(html).toMatch(/color:\s*#c23535/);
  expect(html.indexOf('分析综述')).toBeLessThan(html.indexOf('余额变动'));
  expect(html.indexOf('余额变动')).toBeLessThan(html.indexOf('行动建议'));
});

it('isolates chart measurement and legend from markdown width and word wrapping rules',async()=>{
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true);
  const container=document.createElement('div');const root=createRoot(container);
  const chart=Buffer.from(JSON.stringify({type:'grouped_bar',title:'品类对比',source:'R1',unit:'ratio',points:[
    {label:'POWER',series:'2026-06',value:.0338},{label:'POWER',series:'2026-07',value:.0212},
  ]})).toString('base64');
  await act(async()=>root.render(<RichMarkdown content={`### 变化\n\n正文和证据。\n\n<!-- homenova-chart:${chart} -->\n\n后续解释。`}/>));
  const figure=container.querySelector('figure')!;
  expect(figure).not.toBeNull();
  for(let parent=figure.parentElement;parent && parent!==container;parent=parent.parentElement){
    expect(parent.className).not.toContain('[&_*]:max-w-full');
    expect(parent.className).not.toContain('[overflow-wrap:anywhere]');
    expect(parent.className).not.toContain('[&_ul]:');
  }
  expect(container.querySelector('[data-report-prose]')?.textContent).toContain('正文和证据');
  expect(container.querySelector('[data-report-prose]')?.className).toContain('[overflow-wrap:anywhere]');
  expect(figure.querySelector('details')?.open).toBe(false);
  expect(figure.querySelector('table')?.textContent).toContain('变化幅度');
  expect(figure.querySelector('table')?.textContent).not.toContain('百分点');
  expect(figure.querySelectorAll('th')).toHaveLength(4);
  await act(async()=>root.unmount());
});

it('renders compact units and colored changes through real markdown table components',async()=>{
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true);
  const container=document.createElement('div');const root=createRoot(container);
  await act(async()=>root.render(<RichMarkdown content={'| 指标 | 6月 | 7月 | 变化量 |\n| --- | --- | --- | --- |\n| GMV | 100 EUR | 80 EUR | -20 EUR |\n| 订单 | 10 orders | 8 orders | -2 orders |'}/>));
  const rows=container.querySelectorAll('tbody tr');
  expect(rows[0].textContent).toContain('GMV（欧元）');
  expect(rows[0].textContent).not.toContain('EUR');
  expect(rows[1].textContent).toContain('订单（单）');
  expect(rows[0].querySelector('td:last-child [data-metric-annotation="down"]')?.textContent).toBe('-20');
  await act(async()=>root.unmount());
});

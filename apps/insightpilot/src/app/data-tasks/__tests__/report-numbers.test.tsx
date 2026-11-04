import {it,expect} from 'vitest';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {highlightMovements} from '../components/task-console/ReportNumbers';

it('colors explicit movement without altering dates, source links or code',()=>{
  const html=renderToStaticMarkup(<p>{highlightMovements(<>
    2026-07 PLG-MINI：下降了21.16%，上升8.56%，−3.55。
    <a href="#report-source-R1">下降20%</a><code>value-10</code>
  </>)}</p>);
  expect(html).toContain('color:#c23535');expect(html).toContain('color:#18794e');
  expect(html).toContain('2026-07 PLG-MINI');
  expect(html).toContain('<a href="#report-source-R1">下降20%</a>');
  expect(html).toContain('<code>value-10</code>');
  expect(html).not.toContain('background');expect(html).not.toContain('↑');expect(html).not.toContain('↓');
});

it('leaves signed absolute values neutral unless their column denotes a change',()=>{
  expect(renderToStaticMarkup(<p>{highlightMovements('余额 −100，变化 +20')}</p>)).not.toContain('data-metric-annotation');
  const changed=renderToStaticMarkup(<p>{highlightMovements('−100',true)}</p>);
  expect(changed).toContain('color:#c23535');
  const prose=renderToStaticMarkup(<p>{highlightMovements('下降21%')}</p>);
  expect(prose).toContain('下降<span');
});


it('colors report decline and growth phrases and signed percentages, retaining neutral absolute values',()=>{
  const html=renderToStaticMarkup(<p>{highlightMovements('总会话数微降0.9%，从3.79%降至2.78%（降幅26.6%），订单（-27.4%），客单价上涨8.6%（+8.6%），增幅0.0%，余额-100。')}</p>);
  expect((html.match(/data-metric-annotation="down"/g)??[])).toHaveLength(4);
  expect((html.match(/data-metric-annotation="up"/g)??[])).toHaveLength(2);
  expect(html).toContain('data-metric-annotation="flat"');
  expect(html).toContain('从3.79%降至<span');
  expect(html).toContain('余额-100');
});


it('does not color calendar references after a directional phrase',()=>{
  expect(renderToStaticMarkup(<p>{highlightMovements('销量回升至6月水平；降至2026年低点。')}</p>)).not.toContain('data-metric-annotation');
});


it('colors parenthesized changes and bold directional phrases in both directions',()=>{
  const html=renderToStaticMarkup(<p>{highlightMovements(<>
    转化率<strong>下降8.1%</strong>；<strong>上升8.6%</strong>；降幅（26.6%），增幅（2.4%）。
  </>)}</p>);
  expect(html).toContain('上升<span');
  expect((html.match(/data-metric-annotation="up"/g)??[])).toHaveLength(2);
  expect(html).toContain('降幅（<span');
});

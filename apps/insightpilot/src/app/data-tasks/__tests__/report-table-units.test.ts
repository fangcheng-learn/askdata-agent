import {it,expect} from 'vitest';
import {normalizeReportTableUnits} from '../components/task-console/ReportTableUnits';
it('puts mixed metric units next to metric names without changing values or percentage points',()=>{
 const rows=[['指标','6月','7月','变化量','变化幅度'],['GMV','39,200.44 EUR','30,905.83 EUR','-8,294.61 EUR','-21.2%'],['订单','946 orders','687 orders','-259 orders','-27.4%'],['转化率','3.79%','2.78%','-1.01 个百分点','-26.6%']];
 expect(normalizeReportTableUnits(rows)).toEqual([rows[0],['GMV（欧元）','39,200.44','30,905.83','-8,294.61','-21.2%'],['订单（单）','946','687','-259','-27.4%'],rows[3]]);
 expect(rows[1][1]).toBe('39,200.44 EUR');
});
it('uses column headers for uniform units and keeps ambiguous or mixed currencies explicit',()=>{
 expect(normalizeReportTableUnits([['品类','GMV'],['A','100 EUR'],['B','—']])).toEqual([['品类','GMV（欧元）'],['A','100'],['B','—']]);
 const mixed=[['指标','前期','本期'],['收入','100 EUR','200 USD']];
 expect(normalizeReportTableUnits(mixed)).toEqual([['指标','前期（欧元）','本期（美元）'],['收入','100','200']]);
 const ambiguous=[['指标（美元）','前期（欧元）','本期'],['收入','100 EUR','200']];
 expect(normalizeReportTableUnits(ambiguous)).toEqual(ambiguous);
});

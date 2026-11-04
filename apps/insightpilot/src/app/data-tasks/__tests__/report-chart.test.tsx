// @vitest-environment happy-dom
import { describe, expect, it, vi } from "vitest";
import { chartComparison, chartUnit, decodeReportChart, groupChartPoints } from "../components/task-console/ReportChart";
import { requestReportEvidence, REPORT_EVIDENCE_EVENT } from "../components/task-console/ArtifactMarkdownPreview";

vi.mock("@copilotkit/react-core/v2", () => ({ CopilotChatAssistantMessage: { MarkdownRenderer: () => null } }));

const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64");
const data = { title: "来源图", source: "t%3AR1", unit: "EUR cents", points: [{ label: "A", value: 12000, x: .02 }] };

describe("source-bound report charts", () => {
  it("decodes all supported charts but rejects invalid scatter and negative composition", () => {
    for (const type of ["bar", "grouped_bar", "line", "area", "pie", "donut", "scatter"]) {
      expect(decodeReportChart(encode({ ...data, type }))?.type).toBe(type);
    }
    expect(decodeReportChart(encode({ ...data, type: "scatter", points: [{label:"A",value:2}] }))).toBeNull();
    expect(decodeReportChart(encode({ ...data, type: "donut", points: [{label:"A",value:-2}] }))).toBeNull();
  });
  it("converts units once, with independent axes", () => {
    const y = chartUnit("EUR cents"), x = chartUnit("ratio");
    expect(y.format(12000 * y.scale)).toBe("120");
    expect(x.format(.02 * x.scale)).toBe("2");
    expect(x.label).toBe("%");
  });
  it("keeps missing series null and never sums duplicate observations", () => {
    const grouped = groupChartPoints([{label:"June",series:"A",value:2},{label:"July",series:"B",value:4}]);
    expect(grouped.rows).toEqual([{label:"June",v0:2,v1:null},{label:"July",v0:null,v1:4}]);
    expect(groupChartPoints([{label:"A",value:2},{label:"A",value:3}]).rows[0]?.v0).toBeNull();
  });
  it("routes evidence to a handling panel, retaining inline fallback without one", () => {
    expect(requestReportEvidence("t:R1", "### 数据\n值")).toBe(false);
    const listener = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(REPORT_EVIDENCE_EVENT, listener);
    expect(requestReportEvidence("t:R1", "### 数据\n值")).toBe(true);
    expect((listener.mock.calls[0]?.[0] as CustomEvent).detail).toEqual({sourceId:"t:R1",title:"数据",content:"### 数据\n值"});
    window.removeEventListener(REPORT_EVIDENCE_EVENT, listener);
  });
});


describe("two-period comparison", () => {
  it("pairs periods chronologically, preserving normal and rising items", () => {
    const comparison = chartComparison({...data,type:"grouped_bar",points:[
      {label:"A",series:"2026-07",value:80},{label:"A",series:"2026-06",value:100},
      {label:"B",series:"2026-06",value:100},{label:"B",series:"2026-07",value:120}]});
    expect(comparison?.periods).toEqual(["2026-06","2026-07"]);
    expect(comparison?.rows).toEqual([
      {label:"A",before:100,after:80,delta:-20,percent:-20},
      {label:"B",before:100,after:120,delta:20,percent:20}]);
  });
  it("does not invent periods, aggregate duplicates or divide by zero", () => {
    expect(chartComparison({...data,type:"bar",points:[{label:"A",value:2},{label:"B",value:3}]})).toBeNull();
    expect(chartComparison({...data,type:"bar",points:[{label:"2026-06",value:0},{label:"2026-07",value:2}]})?.rows[0]?.percent).toBeNull();
    expect(chartComparison({...data,type:"grouped_bar",points:[{label:"A",series:"2026-06",value:1},{label:"B",series:"2026-07",value:2}]})?.rows[0]?.delta).toBeNull();
    expect(chartComparison({...data,type:"bar",points:[{label:"2026-06",value:1},{label:"2026-06",value:2},{label:"2026-07",value:3}]})).toBeNull();
  });
  it("keeps percentage-point difference separate from relative percentage", () => {
    const row = chartComparison({...data,type:"bar",unit:"ratio",points:[{label:"2026-06",value:.04},{label:"2026-07",value:.03}]})!.rows[0]!;
    expect(row.delta!*chartUnit("ratio").scale).toBeCloseTo(-1);
    expect(row.percent).toBeCloseTo(-25);
  });
});

import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("@copilotkit/react-core/v2", () => ({
  CopilotChatAssistantMessage: {
    MarkdownRenderer: ({
      content,
      components,
    }: {
      content: string;
      components?: { a?: (props: { href?: string; children?: unknown }) => unknown };
    }) => {
      const Link = components?.a;
      if (Link && content.includes("http")) {
        return createElement(
          "div",
          null,
          createElement(Link, { href: "https://example.com" }, "safe"),
          createElement(Link, { href: "javascript:alert(1)" }, "blocked"),
        );
      }
      return createElement("div", { "data-md": true }, content);
    },
  },
}));

import {
  ArtifactMarkdownPreview,
  isMarkdownFilePath,
  RichMarkdown,
} from "../components/task-console/ArtifactMarkdownPreview";

describe("isMarkdownFilePath", () => {
  it("recognizes common markdown extensions", () => {
    expect(isMarkdownFilePath("notes.md")).toBe(true);
    expect(isMarkdownFilePath("README.markdown")).toBe(true);
    expect(isMarkdownFilePath("doc.MDX")).toBe(true);
    expect(isMarkdownFilePath("data.csv")).toBe(false);
  });
});

describe("RichMarkdown", () => {
  it('hides only a chart title identical to its immediately preceding heading',()=>{
    const encoded=Buffer.from(JSON.stringify({type:'bar',title:'POWER 降幅较大',source:'R1',unit:'EUR',points:[{label:'A',value:10}]})).toString('base64');
    const render=(before:string)=>renderToStaticMarkup(createElement(RichMarkdown,{content:`${before}\n\n<!-- homenova-chart:${encoded} -->`}));
    expect(render('### POWER 降幅较大')).not.toContain('>POWER 降幅较大</span>');
    expect(render('### 2. POWER 降幅较大')).not.toContain('>POWER 降幅较大</span>');
    expect(render('### 二、POWER 降幅较大')).not.toContain('>POWER 降幅较大</span>');
    expect(render('### 其他观点')).toContain('>POWER 降幅较大</span>');
    expect(render('### POWER 降幅较大\n\n另一段说明')).toContain('>POWER 降幅较大</span>');
    expect(render('### POWER 降幅较大')).toContain('EUR');
  });
  it("places a source-bound chart between the Agent's narrative sections and converts cents", () => {
    const encoded=Buffer.from(JSON.stringify({type:"bar",title:"两月销售对比",unit:"EUR cents",source:"t%3AR1",
      points:[{label:"2026-06",value:12000},{label:"2026-07",value:9000}]})).toString("base64");
    const html=renderToStaticMarkup(createElement(RichMarkdown,{content:
      `当前结论\n\n<!-- homenova-chart:${encoded} -->\n\n后续分析`}));
    expect(html.indexOf("当前结论")).toBeLessThan(html.indexOf("两月销售对比"));
    expect(html.indexOf("两月销售对比")).toBeLessThan(html.indexOf("后续分析"));
    expect(html).toContain("EUR");
    expect(html).toContain(">120</td>");
    expect(html).toContain("−25%");
    expect(html).toContain("变化幅度");
    expect(html).toContain("查看数值与变化");
    expect(html).not.toMatch(/<details[^>]*\sopen[ =>]/);
    expect(html).not.toContain("12000");
    expect(html).not.toContain("homenova-chart:");
  });
  it("keeps the report visible and source details collapsed without dropping their content", () => {
    const html=renderToStaticMarkup(createElement(RichMarkdown,{content:
      '# 当前结论\n\n销售减少\n<!-- homenova-report-appendix:v1 -->\n<!-- homenova-source:T%3AR1 -->\n## 原始明细\n完整数据'}));
    expect(html.indexOf('销售减少')).toBeLessThan(html.indexOf('<details'));
    expect(html).toContain('data-report-source="T%3AR1"');
    expect(html).toContain('完整数据');
    expect(html).not.toContain('<details open');
    expect(html).not.toContain('homenova-report-appendix:v1');
  });
  it("scopes Streamdown under data-copilotkit and renders content", () => {
    const html = renderToStaticMarkup(
      createElement(RichMarkdown, { content: "# Hello\n\n- item", density: "chat" }),
    );
    expect(html).toContain("data-copilotkit");
    expect(html).toContain("# Hello");
  });

  it("sanitizes unsafe link protocols", () => {
    const html = renderToStaticMarkup(
      createElement(RichMarkdown, { content: "see http link", density: "artifact" }),
    );
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).not.toContain("javascript:");
    expect(html).toContain(">blocked</span>");
  });
});

describe("ArtifactMarkdownPreview", () => {
  it("applies chrome by default and skips it when bare", () => {
    const chrome = renderToStaticMarkup(
      createElement(ArtifactMarkdownPreview, { content: "body" }),
    );
    const bare = renderToStaticMarkup(
      createElement(ArtifactMarkdownPreview, { content: "body", bare: true }),
    );
    expect(chrome).toContain("max-h-80");
    expect(bare).not.toContain("max-h-80");
  });
});

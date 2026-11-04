"use client";

import React, { type ComponentPropsWithoutRef, type ReactNode } from "react";
import { CopilotChatAssistantMessage } from "@copilotkit/react-core/v2";
import { ReportChart, decodeReportChart } from "./ReportChart";
import {highlightMovements} from "./ReportNumbers";
import {normalizeReportTableUnits} from "./ReportTableUnits";

/**
 * Streamdown (via CopilotKit MarkdownRenderer) ships `cpk:*` utilities that only
 * apply under `[data-copilotkit]`. Without that scope, long paragraphs do not wrap
 * and get clipped by the Outputs pane's overflow-x-hidden.
 *
 * Shared by chat final answers and Outputs MD previews so GFM (tables, lists,
 * code, links) renders consistently with design-system tokens.
 */
// Keep prose rules off chart ancestors. Recharts uses a zero-width measurement
// wrapper; descendant max-width:100% collapses its chart and legend to zero.
const markdownBaseClass = [
  "min-w-0 w-full max-w-full break-words text-foreground [overflow-wrap:anywhere]",
  "[&_*]:max-w-full",
  "[&_p]:my-2 [&_p]:whitespace-normal [&_p]:break-words",
  "[&_li]:my-0.5 [&_li]:break-words",
  "[&_h1]:mb-4 [&_h1]:mt-6 [&_h1]:break-words [&_h1]:text-xl [&_h1]:font-semibold [&_h1]:leading-8 first:[&_h1]:mt-0",
  "[&_h2]:mb-3 [&_h2]:mt-7 [&_h2]:break-words [&_h2]:text-base [&_h2]:font-semibold [&_h2]:leading-7 first:[&_h2]:mt-0",
  "[&_h3]:mb-1 [&_h3]:mt-3 [&_h3]:break-words [&_h3]:text-sm [&_h3]:font-semibold first:[&_h3]:mt-0",
  "[&_h4]:mb-1 [&_h4]:mt-2 [&_h4]:text-sm [&_h4]:font-medium",
  "[&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5",
  "[&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5",
  "[&_blockquote]:my-4 [&_blockquote]:border-l-2 [&_blockquote]:border-primary/40 [&_blockquote]:bg-surface-subtle [&_blockquote]:px-4 [&_blockquote]:py-1 [&_blockquote]:text-muted",
  "[&_hr]:my-4 [&_hr]:border-border",
  "[&_img]:my-2 [&_img]:h-auto [&_img]:max-w-full [&_img]:rounded-md",
  "[&_figure]:mx-0 [&_figure]:my-0 [&_figcaption]:text-foreground",
  "[&_code]:rounded [&_code]:bg-surface-subtle [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.9em] [&_code]:break-words",
  "[&_pre]:my-2 [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border [&_pre]:bg-surface-subtle [&_pre]:p-3",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-[12px] [&_pre_code]:leading-5",
  "[&_table]:my-4 [&_table]:w-full [&_table]:border-collapse [&_table]:text-left [&_table]:text-sm [&_table]:tabular-nums",
  "[&_thead]:bg-transparent",
  "[&_th]:border-x-0 [&_th]:border-t-0 [&_th]:border-b [&_th]:border-border [&_th]:px-4 [&_th]:py-3 [&_th]:font-medium [&_th]:text-muted [&_th]:whitespace-nowrap",
  "[&_td]:border-x-0 [&_td]:border-t-0 [&_td]:border-b [&_td]:border-border/60 [&_td]:px-4 [&_td]:py-3 [&_td]:align-middle [&_td]:text-foreground",
  "[&_tbody_tr:last-child_td]:border-b-0 [&_tbody_tr:hover]:bg-surface-subtle/50",
  "[&_td[data-numeric=true]]:text-right [&_td[data-numeric=true]]:whitespace-nowrap",
  "[&_td[data-change]]:font-semibold",
  "[&_a]:font-medium [&_a]:text-primary [&_a]:underline [&_a]:decoration-primary/30 [&_a]:underline-offset-2 hover:[&_a]:decoration-primary",
].join(" ");

const densityClass = {
  chat: "text-sm leading-7",
  artifact: "text-sm leading-6",
  compact: "text-xs leading-5",
} as const;

/** Card chrome + height cap — used for inline/embedded previews, not the peer page. */
const markdownChromeClass = "max-h-80 overflow-auto rounded-lg border border-border bg-surface p-3";

const streamdownProseClass = "cpk:prose cpk:max-w-full cpk:break-words cpk:dark:prose-invert";

export type RichMarkdownDensity = keyof typeof densityClass;

export function isMarkdownFilePath(path: string): boolean {
  return /\.(?:md|markdown|mdx)$/iu.test(path);
}

function isSafeHttpUrl(href: string | undefined): boolean {
  if (!href) return false;
  try {
    const url = new URL(href, "https://local.invalid");
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export type ReportEvidenceRequest = { sourceId: string; title: string; content: string };
export const REPORT_EVIDENCE_EVENT = "insightpilot:report-evidence";
const EvidenceContext = React.createContext<Map<string, string>>(new Map());

export function requestReportEvidence(sourceId: string, content: string): boolean {
  const title = /^### (.+)$/mu.exec(content)?.[1] ?? "数据明细与计算口径";
  const event = new CustomEvent<ReportEvidenceRequest>(REPORT_EVIDENCE_EVENT, {
    cancelable: true, detail: { sourceId, title, content },
  });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

function SecureMarkdownLink({
  href,
  children,
  ...props
}: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  const sources = React.useContext(EvidenceContext);
  const { node: _node, ...rest } = props;
  const external = isSafeHttpUrl(href) && !href?.startsWith("#");
  // Block javascript: / data: etc. — render as inert text instead of a navigable link.
  if (href && !href.startsWith("#") && !isSafeHttpUrl(href) && !href.startsWith("/")) {
    return <span className="break-all">{children}</span>;
  }
  return (
    <a
      {...rest}
      href={href}
      onClick={(event) => {
        if (!href?.startsWith("#report-source-")) return;
        const sourceId = href.slice("#report-source-".length);
        const evidence = sources.get(sourceId);
        if (evidence && requestReportEvidence(sourceId, evidence)) {
          event.preventDefault();
          return;
        }
        const root = event.currentTarget.closest("[data-report-document]");
        const target = Array.from(root?.querySelectorAll<HTMLElement>("[data-report-source]") ?? [])
          .find((element) => element.dataset.reportSource === href.slice("#report-source-".length));
        if (!target) return;
        event.preventDefault();
        const details = target.closest("details");
        if (details) details.open = true;
        target.focus({ preventScroll: true });
        target.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }}
      {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
    >
      {children}
    </a>
  );
}

function plainText(value: ReactNode): string {
  return React.Children.toArray(value).map(child => React.isValidElement<{children?:ReactNode}>(child)
    ? plainText(child.props.children) : typeof child === "string" || typeof child === "number" ? String(child) : "").join("");
}

const ChangeColumns = React.createContext<number[]>([]);
const TableRows = React.createContext<Map<TableNode,string[]>>(new Map());
type TableNode = {tagName?:string;value?:string;children?:TableNode[]};
function nodeText(node:TableNode):string {return node.value??node.children?.map(nodeText).join('')??'';}
function ReportTableCell({children,node:_node,isChangeColumn=false,...props}: ComponentPropsWithoutRef<"td"> & {node?:unknown;isChangeColumn?:boolean}) {
  const value=plainText(children).trim();
  const numeric=/^[↑↓+−\-]?\s*[€$£]?[\d,.]+(?:%|\s*个百分点)?$/u.test(value);
  const change=isChangeColumn && numeric && /^[↓−-]/u.test(value) ? "down" : isChangeColumn && numeric && /^[↑+]/u.test(value) ? "up" : undefined;
  return <td {...props} data-numeric={numeric || undefined} data-change={change}>{highlightMovements(children,isChangeColumn)}</td>;
}

function ReportTableRow({children,node,...props}: ComponentPropsWithoutRef<"tr"> & {node?:TableNode}) {
  const columns=React.useContext(ChangeColumns);
  const rows=React.useContext(TableRows);
  const normalized=node?rows.get(node):undefined;
  let column=0;
  return <tr {...props}>{React.Children.map(children,child=>{
    if(!React.isValidElement<{node?:TableNode;isChangeColumn?:boolean;children?:ReactNode}>(child))return child;
    const tag=typeof child.type==='string'?child.type:child.props.node?.tagName;
    if(tag!=='td' && tag!=='th' && child.type!==ReportTableCell) return child;
    const index=column++;
    const text=normalized?.[index];
    return React.cloneElement(child,{
      ...(tag==='td'||child.type===ReportTableCell?{isChangeColumn:columns.includes(index)}:{}),
      ...(text!==undefined && text!==plainText(child.props.children)?{children:text}:{}),
    });
  })}</tr>;
}

function ReportTable({children,node,...props}: ComponentPropsWithoutRef<"table"> & {node?:TableNode}) {
  const header=node?.children?.find(n=>n.tagName==='thead')?.children?.find(n=>n.tagName==='tr');
  const columns=(header?.children?.filter(n=>n.tagName==='th')??[]).flatMap((n,i)=> /增减|变化|变动|差额|差值|环比|同比|涨跌|贡献额/u.test(nodeText(n))?[i]:[]);
  const rows=(node?.children??[]).flatMap(section=>section.children??[]).filter(row=>row.tagName==='tr');
  const normalized=normalizeReportTableUnits(rows.map(row=>(row.children??[]).filter(cell=>cell.tagName==='th'||cell.tagName==='td').map(nodeText)));
  const rowValues=new Map(rows.map((row,index)=>[row,normalized[index]]));
  return <div className="my-4 min-w-0 overflow-x-auto" tabIndex={0} role="region" aria-label="数据表">
    <ChangeColumns.Provider value={columns}><TableRows.Provider value={rowValues}><table {...props} className="w-full border-collapse border-0">{children}</table></TableRows.Provider></ChangeColumns.Provider>
  </div>;
}

function ReportTableBody({node:_node,...props}: ComponentPropsWithoutRef<"tbody"> & {node?:unknown}) {
  // Streamdown's bg-muted/40 resolves to our text token, producing a dark slab.
  return <tbody {...props} className="bg-transparent" />;
}

function ReportParagraph({children,node:_node,...props}:ComponentPropsWithoutRef<'p'> & {node?:unknown}) {
  return <p {...props}>{highlightMovements(children)}</p>;
}
function ReportListItem({children,node:_node,...props}:ComponentPropsWithoutRef<'li'> & {node?:unknown}) {
  return <li {...props}>{highlightMovements(children)}</li>;
}
function ReportStrong({children,node:_node,...props}:ComponentPropsWithoutRef<'strong'> & {node?:unknown}) {
  return <strong {...props}>{highlightMovements(children)}</strong>;
}

const reportAppendixMarker="<!-- homenova-report-appendix:v1 -->";
const markdownComponents={a:SecureMarkdownLink,td:ReportTableCell,tr:ReportTableRow,table:ReportTable,tbody:ReportTableBody,p:ReportParagraph,li:ReportListItem,strong:ReportStrong};

type MarkdownRendererProps = {
  content: string;
  className?: string;
  components?: {
    a?: typeof SecureMarkdownLink;
    td?: typeof ReportTableCell;
    tr?: typeof ReportTableRow;
    table?: typeof ReportTable;
    tbody?: typeof ReportTableBody;
    p?: typeof ReportParagraph;
    li?: typeof ReportListItem;
    strong?: typeof ReportStrong;
  };
};

const MarkdownRenderer = CopilotChatAssistantMessage.MarkdownRenderer as (
  props: MarkdownRendererProps,
) => ReactNode;

/**
 * Unified markdown surface for assistant final answers and artifact MD previews.
 * Uses Streamdown (GFM, code highlighting, sanitization) via CopilotKit.
 */
function unnumberedHeading(value?:string) {
  return value?.trim().replace(/^(?:[一二三四五六七八九十]+[、．.]|\d+[、．.])\s*/u,'');
}

export function RichMarkdown({
  content,
  density = "artifact",
  className,
}: {
  content: string;
  density?: RichMarkdownDensity;
  className?: string;
}) {
  const marker=content.indexOf(reportAppendixMarker);
  const body=marker<0 ? content : content.slice(0,marker);
  const appendix=marker<0 ? "" : content.slice(marker+reportAppendixMarker.length);
  const parts=appendix.split(/<!-- homenova-source:([^\s]+) -->/u);
  const sources = new Map(Array.from({length: Math.floor(parts.length / 2)}, (_, i) => [parts[i*2+1]!, parts[i*2+2]!]));
  const blocks=body.split(/<!-- homenova-chart:([A-Za-z0-9+/=]+) -->/u);
  return (
    <EvidenceContext.Provider value={sources}><div
      data-copilotkit
      data-report-document
      className={["min-w-0 w-full max-w-full text-foreground", densityClass[density], className].filter(Boolean).join(" ")}
    >
      <div className="min-w-0 max-w-full overflow-x-auto">
        {blocks.map((block,index)=>index%2 ? <ReportChart key={index} encoded={block}
          hideTitle={Boolean(unnumberedHeading(decodeReportChart(block)?.title)) && unnumberedHeading(/(?:^|\n)#{1,6}\s+([^\n]+)\s*$/u.exec(blocks[index-1]?.trim() ?? '')?.[1])===unnumberedHeading(decodeReportChart(block)?.title)} />
          : <div key={index} data-report-prose className={markdownBaseClass}><MarkdownRenderer content={block} className={streamdownProseClass} components={markdownComponents} /></div>)}
      </div>
      {appendix && <details className="mt-8 border-t border-border pt-4">
        <summary onClick={event => { if (requestReportEvidence("all", Array.from(sources.values()).join("\n\n"))) event.preventDefault(); }} className="cursor-pointer text-sm font-medium text-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">查看数据明细与计算口径</summary>
        {parts[0].trim() && <div data-report-prose className={markdownBaseClass}><MarkdownRenderer content={parts[0]} className={streamdownProseClass} components={markdownComponents} /></div>}
        {Array.from({length:Math.floor(parts.length/2)},(_,i)=><section key={parts[i*2+1]} data-report-source={parts[i*2+1]} tabIndex={-1} className={`${markdownBaseClass} mt-6 min-w-0 overflow-x-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40`}>
          <MarkdownRenderer content={parts[i*2+2]} className={streamdownProseClass} components={markdownComponents} />
        </section>)}
      </details>}
    </div></EvidenceContext.Provider>
  );
}

/**
 * `bare` renders the markdown flush (no bordered box, no height cap) so a
 * dedicated peer page can use the full available height.
 */
export function ArtifactMarkdownPreview({
  content,
  bare = false,
}: {
  content: string;
  bare?: boolean;
}) {
  if (bare) {
    return <RichMarkdown content={content} density="artifact" />;
  }
  return (
    <div className={markdownChromeClass}>
      <RichMarkdown content={content} density="artifact" />
    </div>
  );
}

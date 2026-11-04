import React, {type ReactNode} from "react";

// Direction describes movement, not whether the business outcome is desirable.
export const movementColors = {up:"#18794e", down:"#c23535", flat:"#657182"};
export function movementStyle(direction:keyof typeof movementColors) {
  return {color:movementColors[direction],fontWeight:600,fontVariantNumeric:'tabular-nums' as const};
}

/** Annotate explicit movements only; never infer direction from an isolated value. */
export function highlightMovements(children:ReactNode, allowSigned=false):ReactNode {
  return React.Children.map(children,child=>{
    if(typeof child==='string') {
      const pattern=/(?<![\p{L}\p{N}_])(?:[↑↓+−-]\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:个百分点|%|％|欧元|元|单|件|次|小时))?)|(?:上升|下降|增长|减少|增加|降低|提升|下滑|上涨|下跌|微降|微涨|骤降|暴跌|跌幅|降幅|涨幅|增幅|降至|降到|跌至|跌到|升至|升到|涨至|涨到)(?:了)?(?:约|近)?\s*[（(]?\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:个百分点|%|％|欧元|元|单|件|次|小时))?/gu;
      const parts:ReactNode[]=[];let offset=0;
      for(const match of child.matchAll(pattern)) {
        if(!allowSigned && /^[↑↓+−-]/u.test(match[0]) && !/[%％]$/u.test(match[0])) continue;
        const index=match.index!;
        // A date reference such as ‘回升至6月水平’ is not a movement value.
        if (/^[年月日]/u.test(child.slice(index+match[0].length))) continue;
        parts.push(child.slice(offset,index));
        const zero=Number(match[0].match(/\d[\d,]*(?:\.\d+)?/u)?.[0].replaceAll(',',''))===0;
        const direction=zero?'flat':/^(?:↓|−|-|下降|减少|降低|下滑|下跌|微降|骤降|暴跌|跌幅|降幅|降至|降到|跌至|跌到)/u.test(match[0])?'down':'up';
        const prefix=match[0].match(/^[^\d+−\-↑↓]+/u)?.[0]??'';
        parts.push(prefix,<span key={index} data-metric-annotation={direction} style={movementStyle(direction)}>{match[0].slice(prefix.length).replace(/^[↑↓]\s*/u,'')}</span>);
        offset=index+match[0].length;
      }
      return parts.length?[...parts,child.slice(offset)]:child;
    }
    if(React.isValidElement<{children?:ReactNode;href?:string;node?:{tagName?:string};'data-metric-annotation'?:string}>(child)) {
      const tag=typeof child.type==='string'?child.type:child.props.node?.tagName;
      if(child.props.href || child.props['data-metric-annotation'] || ['a','code','pre'].includes(tag??''))return child;
      if(child.props.children!==undefined)return React.cloneElement(child,{},highlightMovements(child.props.children,allowSigned));
    }
    return child;
  });
}

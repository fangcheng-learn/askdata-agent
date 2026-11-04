// Move repeated display units into a column heading or metric label. Values are
// not converted, inferred or rounded; mixed units remain explicit.
const units:Record<string,string>={EUR:'欧元',USD:'美元',CNY:'元',orders:'单',order:'单',sessions:'次',session:'次',items:'件',hours:'小时',hour:'小时',欧元:'欧元',美元:'美元',元:'元',单:'单',次:'次',件:'件',小时:'小时'};
const numberUnit=/^([+−-]?\s*\d[\d,]*(?:\.\d+)?)\s*(EUR|USD|CNY|orders?|sessions?|items|hours?|欧元|美元|元|单|次|件|小时)$/u;
const missing=/^(?:—|–|-|N\/A|暂无|未取得|本次未取得)$/iu;
function parse(text:string){const m=text.trim().match(numberUnit);return m?{value:m[1],unit:units[m[2]]}:null;}
function label(text:string,unit:string){return /[（(][^）)]*[）)]/u.test(text)?null:`${text}（${unit}）`;}

export function normalizeReportTableUnits(rows:string[][]):string[][] {
  if(rows.length<2)return rows;
  const result=rows.map(row=>[...row]);
  // Single-metric tables: a whole numeric column can share one explicit unit.
  for(let col=1;col<rows[0].length;col++){
    const cells=rows.slice(1).map(row=>row[col]??'').filter(text=>text.trim() && !missing.test(text.trim()));
    const parsed=cells.map(parse);
    if(!cells.length || parsed.some(value=>!value) || new Set(parsed.map(value=>value!.unit)).size!==1)continue;
    const heading=label(rows[0][col],parsed[0]!.unit);if(!heading)continue;
    result[0][col]=heading;
    for(let row=1;row<rows.length;row++){const value=parse(rows[row][col]??'');if(value)result[row][col]=value.value;}
  }
  // Mixed-metric overview: each row has its own unit (GMV, orders, visits).
  for(let row=1;row<rows.length;row++){
    const cells=result[row].slice(1).filter(text=>text.trim() && !missing.test(text.trim()) && !/[%％]|百分点/u.test(text));
    const parsed=cells.map(parse);
    if(!cells.length || parsed.some(value=>!value) || new Set(parsed.map(value=>value!.unit)).size!==1)continue;
    const heading=label(result[row][0],parsed[0]!.unit);if(!heading)continue;
    result[row][0]=heading;
    for(let col=1;col<result[row].length;col++){const value=parse(result[row][col]);if(value)result[row][col]=value.value;}
  }
  return result;
}

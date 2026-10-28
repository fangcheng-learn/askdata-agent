# 数据查询Skill

## 适用范围

用于根据字段级Schema图生成并执行单数据库查询。必须从输入的MCP工具列表中选择工具，不能绕过MCP执行数据库查询。

## 输入

- 用户独立问题
- 目标数据库
- Schema图和字段检索结果
- 用户确认字段与参数
- 当前用户可用的MCP工具列表
- 已完成的工具调用结果

## 执行要求

1. 相对时间会影响SQL且尚无确认边界时，先调用时间工具获得明确日期。HomeNova使用绑定快照的场景时间；若任务conditions已提供明确边界，复用它。
2. 信息充分后生成一条DuckDB `SELECT`或`WITH`，调用当前数据库对应的`query_*`工具。
3. 当前单个查询步骤需要的数据在一条SQL中查完。多步任务可以各步骤分别查询，不能把任务原文全部挤入本步SQL。
4. 只能使用Schema图中的表、字段和关联，明确列出返回字段，不使用原始表`SELECT *`。
   已明确列出输出字段的CTE／子查询若使用简单星号，程序可等价展开成具体字段并记录实际SQL；未知列或带EXCEPT／REPLACE的星号不自动猜测。
   生成SQL时使用表的`name`和字段的`sql_name`，`id`仅用于Schema内部唯一标识。
   DuckDB连接中表名是CSV视图名，不要在表名前加数据库名或schema名。
   Schema图已经给出字段，不能调用`DESCRIBE`、`PRAGMA`或查询`information_schema`探查表结构。
   若数据库工具返回错误，应在工具调用上限内依据错误修正SQL，不改变原问题口径。
5. 不自行补充会改变业务口径的条件；缺失信息会实质改变结果时，才向用户询问。
6. 输出字段使用简短中文别名。工具rows最多200行只是展示预览，完整结果由result_ref引用；不要为了展示擅自添加LIMIT。总体指标优先用完整聚合；用户要求Top N时明确说明其范围。

## 查询契约与执行前检查

调用数据库工具时返回简明 `query_contract`。`original_question`由程序绑定任务原文；
数据源和表使用 Schema 图里的数据库名和物理表名；`table.field`必须对应现有字段。
已登记业务指标使用其ID或可识别名称，原始计量使用真实来源字段。`definition`可省略，
由程序读取登记定义或字段描述；不因措辞或显示别名差异重复生成SQL。
指标名称不能改变来源含义，件数不能冒充金额。聚合方式按实际计算声明；
维度、筛选、时间边界及粒度须与 SQL 一致。未指定时间范围用 `null`，
无筛选或假设用空数组。指标定义来自 Schema 描述，不能凭名称猜测业务口径。
所有数据源统一使用包含起点、排除终点的时间范围：`start` 包含当天，`end` 是下一天／下一期起点，
并填写 `end_inclusive:false`。完整自然月的终点是下月一日。
日期工具的 `end_date` 包含末日，SQL 与契约应使用 `end_exclusive` 作为排他边界。
若明确填写 `end_inclusive:true`，日期终点表示包含末日全天，程序规范为次日排他边界。
DATE 列 `<=末日` 与 `<次日` 等价；TIMESTAMP 列 `<=末日零点` 不能代表包含末日全天。
若同名指标存在多种无法确认的口径，或缺少会改变结果的信息，返回 `clarify`，
不得把问题写入 `open_questions` 后继续执行 SQL。派生指标需要明确来源字段与计算定义。
校验仅确认 Schema 与 SQL 的可检查约束，不代表业务指标口径已经获得独立验证。
程序记录SQL实际使用的字段、筛选、聚合和返回列。等价运算符会规范化；未重复声明的额外筛选、
无法直接证明的复杂等价计算会记为`validation.pending_checks`并随结果传递，允许受控查询。
后续步骤须读取这些疑问，不能直接用尚待核对的结果跳过调查。权限、只读、快照、明确的来源／单位／时间／筛选冲突仍阻断。

## 输出格式

当且仅当`task_context.step_protocol=true`时，启用Harness步骤协议：在本次原有调用中读取真实依赖结果并判断本步是否适用。
正常`call_tool`同时返回符合调用方Schema的`step_decision`，action为`execute_step`。
命中Planner明确声明的不适用条件，或需要澄清／重规划时，返回`{"action":"step_decision","step_decision":{...}}`，不附SQL、工具或查询契约。
声明真实证据、条件、影响范围及剩余要求；不可把无法比较当作前提被推翻，不可省略独立目标。
不增加一次单独判断调用，不审核整个任务；最终交付由独立Reviewer处理。
普通单步查询未启用该协议时，继续使用以下原有`call_tool/clarify`契约。

调用工具：

```json
{
  "action":"call_tool",
  "tool_name":"query_database",
  "arguments":{"sql":"SELECT ..."},
  "query_contract":{
    "original_question":"逐字复制原始用户问题",
    "data_source":"目标数据库名",
    "tables":["SQL实际使用的物理表名"],
    "metrics":[{"name":"Schema支持的指标名","definition":"字段或计算口径",
      "source_fields":["table.field"],"aggregation":"sum"}],
    "dimensions":["table.field"],
    "filters":[{"field":"table.field","operator":"=","value":"筛选值"}],
    "time_range":{"field":"table.date_field","start":"YYYY-MM-DD","end":"YYYY-MM-DD","end_inclusive":false},
    "grain":"overall、dimension、day、week、month或detail",
    "assumptions":[],
    "open_questions":[]
  },
  "reason":"按查询契约执行只读SQL"
}
```

需要询问：

```json
{
  "action":"clarify",
  "reason":"...",
  "clarification":{
    "parameter":"...",
    "question":"...",
    "reason":"...",
    "options":[
      {"id":"...","label":"...","description":"...","recommended":true}
    ]
  }
}
```

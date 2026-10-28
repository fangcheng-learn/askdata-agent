# AskData 通用版修复后回归验证

验证日期：2026-10-25。仅针对 `references/askdata_studio`；未移植运营版代码，未增加 Planner，未重构工作流。证据标签：**真实 API** 为正在运行的 `127.0.0.1:8001`；**本地 DuckDB** 为同一份电商 CSV 的独立只读核算；**Mock** 为脚本化模型回答，工作流、MCP 和 DuckDB 仍使用实际代码。

## 1. 代码变更核对

因为 `references/` 尚未纳入 Git，本次以用户最初下载目录 `/Users/fangchengdemac/Downloads/askdata_studio/backend` 为逐字节对照基线。应用层仅有下列 7 个文件与原版不同；本轮的小修复包含在其中。测试层修改 `tests/test_service.py`、`tests/test_ecommerce_dataset.py`，新增 `tests/test_model_client.py`。

| 文件与函数 | 实际实现和原因 |
|---|---|
| [`config.py:34`](../references/askdata_studio/backend/app/config.py#L34) `Settings.llm_enable_thinking` | 默认 `false`，可用 `LLM_ENABLE_THINKING` 覆盖；`/api/health` 实际返回 `false`。上轮对照测试显示关闭思考模式与复杂请求耗时下降有关，但**不能证明**先前 HTTP 403 的上游根因。 |
| [`model_client.py:25`](../references/askdata_studio/backend/app/model_client.py#L25) `chat()`、[`_post()`](../references/askdata_studio/backend/app/model_client.py#L91) | Chat payload 显式传 `enable_thinking`；HTTP 4xx（429 除外）不重试、不触发 30 秒熔断；429、5xx 和网络故障按配置重试/熔断。上游 HTTP 错误保留完整响应体、状态和原因，供再现时取证。 |
| [`retrieval/graph.py:18`](../references/askdata_studio/backend/app/retrieval/graph.py#L18) `SchemaGraphBuilder.build()` | 时间问题可从**已经选中的真实 Schema 表**补入 `role=time` 字段；没有写死 `stat_date`，也不创造新字段。 |
| [`workflows/query_graph.py:191`](../references/askdata_studio/backend/app/workflows/query_graph.py#L191) `_retrieve_schema()`、[`_after_retrieval()`](../references/askdata_studio/backend/app/workflows/query_graph.py#L241) | 依据预处理的 `time_expressions` 启用时间字段补充。本轮增加零命中保护：无可用字段则直接结束为 `failed/schema_not_found`，不进入 SQL Agent。 |
| [`querying/single_database_agent.py:28`](../references/askdata_studio/backend/app/querying/single_database_agent.py#L28) `prepare()`、[`_compact_field()`](../references/askdata_studio/backend/app/querying/single_database_agent.py#L155) | 精简重复 Schema 检索内容；数据库工具报错时将错误与原 SQL 回传模型，最多调用 `min(MCP_MAX_TOOL_CALLS, skill.max_tool_calls)` 次，当前实际为 3。首条**成功** SQL 后立即返回，不会做业务方向性续查。 |
| [`skills/database_query/SKILL.md:20`](../references/askdata_studio/backend/app/skills/database_query/SKILL.md#L20) | 提醒模型仅用 Schema 图中的表/字段/关联、CSV 视图名，禁止 `DESCRIBE`/`information_schema` 探表，并按执行错误修 SQL。它是模型约束提示，**不是确定性的字段级 SQL 校验器**。 |
| [`workflows/result_builder.py:15`](../references/askdata_studio/backend/app/workflows/result_builder.py#L15) `schema_not_found()`、[`direct_response()`](../references/askdata_studio/backend/app/workflows/result_builder.py#L59)；[`query_graph.py:294`](../references/askdata_studio/backend/app/workflows/query_graph.py#L294) `_execute_single_database()` | 预处理模型不可用、Schema 零命中返回 `failed`。本轮补齐“SQL 已成功、结果说明模型超时”场景：保留 SQL 和行数据，但任务标为 `failed`，错误进入 `execution_log`。 |

**数据与权限完整性（本地只读比对）：**电商目录 52 个数据/元数据文件、`data/schema_store.json`、`app/database.py`、`app/security/access_control.py`、`app/security/auth.py` 均与原始下载目录逐字节一致。详见[基线与完整性记录](askdata_audit_evidence/studio_regression_baseline.json)。本轮没有写入电商 CSV、Schema 或权限文件。

## 2. 基础查询结果

全部原始请求、响应、SQL、`execution_log`、MCP 工具结果、错误和耗时保存在[真实 API JSONL](askdata_audit_evidence/studio_regression_live.jsonl)。完整表清单、独立核算 SQL 和数据完整性保存在[本地 DuckDB 基线](askdata_audit_evidence/studio_regression_baseline.json)。真实 API 均返回 HTTP 200；任务成功/失败要看响应体的 `status`。

| 输入与路径 | 真实结果 | 独立核对/判断 |
|---|---|---|
| `GET /api/schema`，销售分析员权限 | 0.008 秒，返回电商 50 张表。 | 与 DuckDB `information_schema.tables` 中的 50 个视图名完全一致。完整名称在基线 JSON。**通过（元数据 API）**。 |
| “请列出当前电商数据库有哪些数据表，只需列出表名。” | 4.098 秒，`completed/direct_response`，无 SQL/工具调用；回答称无法直接列出表名。 | **自然语言表清单未通过**。它没有利用已有 Schema API；`completed` 只表示直接回答结束，不表示答对。 |
| “查询电商商品日运营指标表 daily_product_metrics 中非空 GMV 数据的最早和最晚统计日期 stat_date。” | 11.131 秒，1 次 `query_ecommerce_ops`；`SELECT MIN(stat_date), MAX(stat_date) ... WHERE gmv IS NOT NULL`；返回 **2025-08-23～2026-08-22**。最终重启后同问 12.809 秒再次成功。 | 独立 DuckDB 核算相同，非空 GMV 25,000 行。**通过（2/2 样本）**。 |
| “请按商品类目统计 2026 年 8 月 1 日至 8 月 22 日的 GMV，返回类目名称和 GMV，按 GMV 降序列出前 5 名。” | 三次真实调用分别 16.266、16.050、16.931 秒，均 1 次成功的 `query_ecommerce_ops`。前五为女装 **13,876,611.90**、箱包配饰 **13,157,495.73**、童装童鞋 **12,919,731.39**、个人护理 **12,761,734.49**、护肤 **12,601,082.88**。 | 三次均与独立 DuckDB 汇总在 0.01 元容差内一致。**通过（3/3 样本）**。 |

**日期字段来源：**静态 Schema 中 `ecommerce_ops.daily_product_metrics.stat_date` 存在且 `role=time`；真实 API 返回的 `schema_graph.fields` 中也有该字段。类目 GMV 请求中的该字段来源标为 `time_context`，日期覆盖范围请求中来源标为 `retrieval`。实际 SQL 均使用 `dpm.stat_date` 或 `stat_date`；这不是只能靠模型猜测才能得出的字段。应注意 Schema 图还包含其他已选表的时间字段，系统目前未强制验证模型选用的事实表是否符合指标口径。

## 3. 失败恢复结果

完整输入、SQL、逐次 MCP 调用、数据库 Binder Error、输出和耗时在[可控 Mock JSONL](askdata_audit_evidence/studio_regression_mock.jsonl)。这里模型回答由测试桩生成，**不代表真实模型在自然错误下的修复成功率**。

| 可控输入 | 轨迹与结果 |
|---|---|
| 第 1 次 SQL 为 `SELECT missing_column FROM orders_current`，第 2 次给出有效 SQL | 真实 DuckDB 第 1 次返回 `Binder Error: Referenced column "missing_column" not found`；错误进入下次模型观察；第 2 次 SQL 成功，4 行，`status=completed`，2 次 MCP 调用，0.487 秒。**有限修复机制生效**。 |
| 每次都输出上述错误列 | 三次 MCP 调用均失败；第 3 次后 `status=failed`，没有第 4 次；最后的 SQL 和 Binder Error 保留在响应，0.470 秒。**上限生效**。 |
| 预处理模型抛“连接超时” | `failed/route_fallback`，无 SQL，`execution_log.route_fallback.success=false`，0.001 秒。 |
| SQL 已成功后结果说明模型抛“连接超时” | MCP SQL 成功并返回 4 行；`result_analysis.success=false`；任务 `failed`，SQL/行数据仍可查看，0.407 秒。此为本轮修复的状态语义。 |
| 强制 Schema 零命中 | `failed/schema_not_found`，`sql=null`，0 次 MCP 调用，0.001 秒。真实未知指标请求在修复前错误地走到 `mcp_tool_discovery`，修复后 4.997 秒停在 `schema_retrieval`；最终重启后 4.704 秒再次复现。 |

HTTP 403 的客户端单元测试确认：非重试 403 不触发熔断，下一次正常请求可成功；新增长响应体测试确认错误文本不会在 500 字处截断。[67 个测试的运行记录](askdata_audit_evidence/studio_regression_tests.txt)。**本轮真实 API 请求未出现模型 HTTP 403**，因此没有本轮真实上游 403 响应可归因或展示。若再次发生，应保存当次供应商状态、原因和完整响应体；现有响应仍不能证明其内部原因。

## 4. 当前能力边界与结论

- **单次查数：初步可用。**本轮日期范围 2/2、类目 GMV 3/3 成功且数值与独立核算一致；样本少、问题明确，不能推断生产稳定性。自然语言表清单仍失败。
- **SQL 执行错误修复：机制已验证，真实模型成功率未验证。**仅在数据库工具返回失败时，最多 3 次；成功执行后不会因口径错误、重复计数或证据不足再查。
- **Schema 零命中：现在会停止。**这只覆盖“零命中”；如果召回错误但非空，提示词无法保证模型不使用图外字段。数据库层会拦截未知表，未知列通常到 DuckDB 才报错。
- **任务状态：**预处理故障及结果说明故障均不会再误标 `completed`。SQL 成功但说明失败时保留数据，并明确标记整体任务失败。
- **主动第二次方向性调查：没有。**[`SingleDatabaseAgent.prepare()`](../references/askdata_studio/backend/app/querying/single_database_agent.py#L124) 在首条成功数据库结果后返回，[`QueryWorkflow._compile()`](../references/askdata_studio/backend/app/workflows/query_graph.py#L59) 将执行节点直接连到 `END`。多次 MCP 调用仅可能是时间工具或 SQL 错误重试；不是根据 GMV 结果选择新方向。**Planner 与动态重规划仍缺失。**

下一阶段先补“列出可见表”的确定性元数据路径，并把 GMV 权威事实表、时间粒度和类目聚合口径写成可校验契约；当前 `daily_product_metrics` 与 `daily_store_metrics` 的 GMV 合计并不相同。随后用真实模型跑一组受控 SQL 错误与 Schema 误召回评测，观察修复成功率和误答率。完成这些基础验证后，再决定多步调查控制器的设计；本轮没有实现 Planner。

## 5. 管理员“本月各地区销售额”截图追查（同日追加）

前述单次查数成功样本使用仅能访问电商库的销售分析员账号，不能外推到拥有全部数据库权限的管理员。对截图中的原句“查询本月各地区销售额”做管理员真实 API 复现：Schema 图同时包含 `askdata_mock` 和 `ecommerce_ops`（共 11 张候选表），直接进入未实现的多库 Handoff，HTTP 200 的任务响应为 `failed`，无 SQL，耗时 4.971 秒。[修复前原始记录](askdata_audit_evidence/studio_admin_multidb_repro.json)。这不是模型账单或连接错误。

针对这个歧义，通用版 [`_retrieve_schema()`](../references/askdata_studio/backend/app/workflows/query_graph.py#L191) 现在使用现有的 Human-in-the-loop 路径询问“电商经营数据”或“示例销售数据”；选定后仅保留该库的检索字段，再继续单库查询。没有实现跨库查询，也没有增加 Planner。新增管理员选择/恢复与空结果状态测试后，后端总计 **69 个测试通过**。[测试记录](askdata_audit_evidence/studio_multidb_fix_tests.txt)。

真实 API 再测同一句：4.732 秒返回 `waiting_clarification`，选择电商库后生成 2026-10-01～2026-10-31 的 SQL，执行成功但返回空行。[初次修复轨迹](askdata_audit_evidence/studio_admin_multidb_after_fix.jsonl)。原因是当前日期为 2026-10-25，而示例销售订单只到 2026-08-31，电商订单只到 2026-08-22。随后增加空结果状态处理：SQL 成功且返回 0 行时保留查询记录，并显示“查询完成，但无匹配数据”；最终真实 API 复测为 4.978 秒询问数据库、在聊天框回复“电商经营数据”后 7.254 秒返回该状态。[最终原句轨迹](askdata_audit_evidence/studio_admin_multidb_final.jsonl)。

把日期明确为 2026-08-01～2026-08-22 后，同一管理员会话先收到数据源澄清；在聊天框回复“电商经营数据”，任务成功执行一条 `orders` 查询并返回 5 个地区，API 行数据与独立 DuckDB 执行完全一致。[有数据时的真实轨迹](askdata_audit_evidence/studio_admin_multidb_august.jsonl)。

前端的三个预置提问原先都使用“本月”，与本地数据覆盖范围不符；现改为 2026 年 8 月的明确日期或日期覆盖问题。[示例提问配置](../references/askdata_studio/frontend/src/App.vue#L27) 已通过 `vue-tsc -b` 和 Vite 生产构建。多库歧义仍由用户选择数据来源，不会暗中选库。

# DataFoundry 自然语言查询链路

依据官方源码提交 `5cf7d364e2eddf69f84a1ac2eb0a606e069c67a4` 和本地首题事件。建议从 `packages/agent-runtime/src/index.ts` 的 `createDataFoundry` 开始学习。

1. Web 的 `apps/web/src/app/data-tasks/data-tasks-app.tsx` 调用 CopilotKit `runAgent({ agent, forwardedProps })`；`data-task-state.ts` 生成 `run_config`，包括数据源和模型选择。正式 Web 经 `apps/web/src/app/api/copilotkit/[[...path]]` 同源代理到 API。
2. `apps/api/src/server.ts` 的 `handleCopilotKitRequest` 接收 `agent/run`、认证用户与配置。`apps/api/src/run-agent-assembly.ts` 的 `createRunAgentContext` 和 `createRunAgentAssembly` 创建运行上下文，把 Data Gateway、模型、消息与资源策略交给 `createDataFoundry`，再包装成 `MastraAgent`。
3. `packages/agent-runtime/src/index.ts` 的 `createDataFoundry` 选择数据工具、建立 data-analysis 协议边界，通过 `governedToolFactory.governTools` 包装工具，创建 Mastra `Agent`。`packages/agent-runtime/src/tools/data-tools.ts` 的 `createDataFoundryToolRegistry` 提供 `list_data_sources`、`inspect_schema`、`preview_table`、`run_sql_readonly`。SQL 工具要求同次运行先取得 `schema_id`。
4. `packages/data-gateway/src/index.ts` 的 `DataGateway.runSqlReadonly` 执行只读 SQL guard、表策略、行数与超时限制，写 SQL 审计、查询历史和表格产物。`packages/data-gateway/src/adapters/local-sql-adapters.ts` 的 `DuckDbAdapter` 用 `information_schema.columns` 抓字段，用 Node `duckdb` 模块执行 SQL。
5. AG-UI 事件流返回工具调用、步骤快照、文本和表格；Web 的 `data-tasks-app.tsx`、`live-run-state.ts`、`tool-result-normalize.ts` 将其显示。首题实测包含 `RUN_STARTED`、`inspect_schema`、`run_sql_readonly`、25 行表格、审计 ID 和 `RUN_FINISHED`，原始事件在被 Git 忽略的 `data/processed/phase1_probe.json`。

首题也展示了模型局限：它先猜 `orders`/`order_items` 导致空 schema，随后重查真实九表；SQL 虽执行成功，部分月份又被 `COALESCE` 成 0。平台保证访问边界与可追溯性，业务口径仍需按 [指标定义](metric_definitions.md) 复核。

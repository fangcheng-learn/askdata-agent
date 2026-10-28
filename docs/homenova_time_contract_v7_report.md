# 八月 GMV 时间边界误拦：实际记录与修复

日期：2026-10-28。开发资料，不进入运行时知识。

## 本次真实失败

任务 `5d857a88b6f6`，用户问题“2026年8月GMV”，数据源 `ecommerce_ops`。先通过前端模板热更新显示已有错误详情，再按用户权限 GET 现有任务保存记录；没有重发问题，没有重启后再猜原 SQL。

| 候选 | 实际 SQL 上界 | 声明的 end | 原结果 |
|---|---|---|---|
| 1 | `stat_date < '2026-09-01'` | `2026-08-31`，未定义包含性 | 被误拦 |
| 2 | `stat_date <= '2026-08-31'` | 同上 | 被误拦 |
| 3 | 与候选 2 相同 | 同上 | 同一错误停止 |

三份 SQL 均从 `2026-08-01` 起，聚合 `daily_store_metrics.gmv`。该列的时间字段是 DATE，两种上界都包含整个八月的查询范围。**第一条 SQL 已经正确，校验与模型生成接口不一致造成了无效修复。** 原任务有 4 次 Chat（入口一次、Coder 三次）、Embedding 与 Rerank 各一次，没有执行任何业务 SQL；本轮没有新增这些付费请求。

直接原因：查询契约 `end` 没有声明包含性，普通链路 Skill 也没说清楚，校验器却强制用 `< end`。左闭右开的解释只给了 HomeNova，漏掉普通查数。同时，日期工具原有 `end_date` 明确包含末日。原请求没有调用日期工具，不能把本次 end 值归因于实际复制了工具输出；它说明整个接口约定本身没有统一。

## 修复

- 所有数据源的通用提示、数据库 Skill 和查询契约明确终点语义；新声明使用 `end_inclusive:false` 的排他终点。
- 日期工具保留原 `end_date` 兼容，新增代码计算的只读 `end_exclusive`；真实 MCP 输出 Schema 与实际返回都包含该字段。
- `end_inclusive:true` 的日期包含末日全天，规范为次日排他边界。DATE 的 `<=末日` 可证明等价于 `<次日`；TIMESTAMP 的 `<=末日零点` 不会按同一规则认证全天。
- 旧格式没写包含性时，只有实际 SQL 支持“含末日／排他次日”的对应关系才规范化；记录原值、规范值与待核对项。不会无条件给所有 end 加一天，也不改写原 SQL。
- 旧格式即便 SQL 使用 `<同日`，仍标明包含性未确认，不能凭模型两份自述一致就认证完整月份。明确排他边界冲突、任意多查几天、权限与字段等保护保留。
- 支持包含末日的单日范围 `start=end`，显式排他的空范围仍拒绝。
- 失败页新增可展开的“错误详情”，展示任务编号与已有校验候选，便于以后直接定位，不必让用户重跑。

## 实际查询与数据覆盖

本轮仅回放已保存候选：3 份校验通过，2 种不同 SQL 各在本地执行一次，重复候选复用结果。查询结果与标准库 CSV Decimal 独立核算一致。

**店铺日经营表当前仅有 2026-08-01—08-22 的八月观测，共 22 天、1,760 行；可见 GMV 合计 107,191,658.39 CNY。** 表的最晚日期是 08-22，不能把 08-23—08-31 没有记录当成零，也不能据此报告完整八月的最终 GMV。

当前实现把登记的最晚观测日与请求期间比较；超出时传递 `time_coverage_not_proven` 给最终审核。它不单凭最后一条业务记录断言后续一定缺数，而是明确后续日期未被证明覆盖，交付需说明可见范围。

## 验证与限制

- 后端 248 项、E 适配 14 项通过；前端类型检查与生产构建通过。
- 新增 18 项时间专项测试，涵盖实际候选写法、显式包含／排他、旧格式、DATE／TIMESTAMPTZ、闰年、跨年、单日与覆盖说明。单测使用脱敏最小 fixture，不依赖私有历史文件。
- 既有明确排他负例补上 `end_inclusive:false`；拒绝标准不变。旧格式的未定义含义不会再假装等于显式排他约定。
- 510 份受保护文件、5 份既有真实记录／台账指纹未变；原第二题暂停保留。原失败任务不回填成功。
- 本轮新增真实模型、Embedding、Rerank、付费 Agent 调用均为 0。没有重新进行真实模型完整业务验收，不以本地 SQL 通过代替它。
- 没有增加过程 Reviewer、修改预算、重建数据库或替换数据源。按原启动器更新 8001／5174，桌面启动图标继续可用。

[原任务原始记录](../data/homenova_harness_v1/private/time_contract_v7/actual_failed_task_5d857a88b6f6.json) · [实际候选查询及独立金额](../data/homenova_harness_v1/private/time_contract_v7/replay_actual_candidates.json) · [最终候选校验与保护检查](../data/homenova_harness_v1/private/time_contract_v7/release_validation.json) · [248 项后端回归](../data/homenova_harness_v1/private/time_contract_v7/backend_release.txt) · [E 适配](../data/homenova_harness_v1/private/time_contract_v7/phase_e.txt) · [前端构建](../data/homenova_harness_v1/private/time_contract_v7/frontend_build.txt)

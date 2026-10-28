# 原因请求被年份澄清吞掉：定位与 Flash 接入

日期：2026-10-29。开发资料，不进入运行时知识。

## 实际运行的原因

最近任务 `31ec175c7c77`。上一句为“分析六月份相较于5月GMV”，入口模型将它改写为“查询[指定年份]年5月和6月的GMV总额，并计算环比变化”，`needs_planner=false`，随后等待选择年份。

用户补充的完整问题是“2026年6月份相较于5月份GMV下降的原因”。代码在进入预处理前，发现该句含选项标签“2026年”，就当成原任务的年份答案，转换成 `year=2026` 并继续旧流程。它既没有把整句交给路由模型，也没有创建原因调查计划。

原任务的实际 `standalone_query` 最终仍为旧月份对比加年份，SQL 仅计算5月、6月总额及环比，结果下降4.55%。这回答了“下降多少”，没有调查“为什么下降”。预算中也只有最初一次 `preprocess_decision`，原因请求没有再次进入路由。原任务累计 Chat 5、Embedding 2、Rerank 2。

问题在澄清入口的两处实现：选项标签只要在整句话中出现就匹配；非 Schema 选项又被一律当作可自由输入的参数兜底。因此，仅换路由模型不能修复这个绕过入口的分支。

## 实际修改

- 普通选项澄清只接受完整选项 ID／标签、序号或简短选择用语。含额外新目标的完整问题不会只因为出现年份被消费。
- 多个明确选项不能再落入任意文本参数兜底；完整新问题走正常入口，以新任务保留全部目标。单值输入兼容保留；Harness 原有澄清恢复路径不变。
- 只回复“2026年”等纯澄清时，恢复原任务和原预算，不重复预处理。
- 入口提示强调：继承同会话中唯一明确的年份；结果覆盖不全不等于年份参数不可复用；原因目标不得被改写成仅比较数字。它是提示改进，不表示真实模型行为已通过新验收。
- `ROUTE_MODEL` 独立配置，默认 `qwen3.7-flash`，路由关闭 thinking；显式留空沿用主模型。只影响入口的上下文聚合、路由、Skill 选择与检索参数提取。
- Planner、SQL、问答、草稿、Reviewer 保持主模型配置；Embedding、Rerank、连接、安全、预算、超时与重试上限保持。
- 所有有执行日志的结果可展开“运行过程”，便于查看已有任务编号与记录。

Flash 型号按[阿里云官方模型信息](https://help.aliyun.com/zh/model-studio/qwen3-7-flash)核对，未发起真实服务探测，不改共享密钥文件。

## 验证与边界

- 新增6项澄清入口回归：整句原因完整进入预处理并分派到 Harness，旧普通 SQL 执行0；纯年份同任务／同 RunBudget 恢复，预处理仅首次一次，保留既有主动时间与工具次数。
- 新增4项模型布线回归：捕获实际客户端待发送 payload，只有入口是 Flash，下游仍为原模型，预算记录实际模型；所有网络均 mock。
- 后端258项、E适配14项、前端类型检查与构建通过。
- 510份受保护文件、5份旧真实运行／台账指纹不变；待运行第二题继续暂停。没有提交新的`/api/query`或真实模型请求，也没有重发原问题。全量回归包含既有本地CSV SQL测试，不将它们记成新的真实模型验收。
- 受控 Harness 分派不等于真实 Flash 路由准确率或完整业务调查验收；尚未新增真实模型试运行，不回填原任务为调查成功。

原启动器更新后，实际健康接口已确认入口模型为`qwen3.7-flash`、主模型仍为`qwen3.7-plus`。[部署配置检查](../data/homenova_harness_v1/private/routing_v8/deployed_checks.json)。重启后按既有规则重新登录。

[原任务与实际入口决策](../data/homenova_harness_v1/private/routing_v8/actual_task_31ec175c7c77.json) · [模型 payload 专项](../data/homenova_harness_v1/private/routing_v8/route_model.txt) · [全部后端回归](../data/homenova_harness_v1/private/routing_v8/backend.txt) · [E适配](../data/homenova_harness_v1/private/routing_v8/phase_e.txt) · [前端构建](../data/homenova_harness_v1/private/routing_v8/frontend_build.txt) · [保护检查](../data/homenova_harness_v1/private/routing_v8/protection_check.json)

# 按需业务Skill与主Agent提示词修订

2026-10-29。修改对象为references/askdata_studio，后端8001、前端5174。新增应用模型API调用0、真实Agent试跑0；不包含Codex开发对话用量。

## 实际修改

- **业务Skill改为按需方法。** 移除“没有加载业务Skill就禁止调查SQL执行”的检查。Agent仍自己选Skill；复杂调查优先读取，也允许先核实数据再补读。SQL模块继续自动提供database_query技术规则，真实权限、只读、字段、口径、依赖与预算检查保留。
- **主提示词重整。** 按目标与每轮决策、方法选择、范围与证据、调查记录、动作、示例、反馈／预算／交付组织。减少GMV细分方法重复，具体指标关系与取证分支留在业务Skill。增加先读Skill、先查事实两种完整JSON示例，以及core_questions／explanations的记录模板；示例不预填私有业务数字，真实引用必须来自当前目录。
- **准备与SQL错误分开。** Skill读取、检索和日期准备错误反馈preparation_error，不进入SQL失败缓存、不扣SQL生成次数。反复无效准备仍服从原协议修复及时间／工具预算。明确的权限拒绝用permission分类，不交给SQL重写；普通查数与调查均适配。每条SQL总共3次、业务取证5轮、工具20次和时间限制未放宽。
- **配置页同步。** 当前主提示词与页面共用定义，加载规则明确业务Skill可选、查询技术Skill自动提供；没有改写线上自定义覆盖。

主要代码：workflows/react_prompts.py、workflows/react_harness.py、querying/query_contract.py、querying/query_protocol.py、querying/single_database_agent.py及配置目录说明。三份业务Skill正文未为了某个案例写专门答案。

## 原首条SQL的离线回放

输入为真实失败任务4fab6710cf46：“7月GMV怎么比6月少了？感觉销量明明还可以啊”。原候选SQL及契约未改，在新建临时任务、skills={}、临时结果存储中，经共享SQL模块、原查询校验和真实本地MCP执行；没有调用模型。

移除Skill拦截后，回放先后暴露两处原契约兼容问题，拒绝记录均保留：

1. 两个CTE都输出period，最终SELECT明确选择pm.period。原规范化器却先搜索所有输入，误判为多来源。现按最终唯一输出表达式解析；多物理来源、重复输出或无法证明的映射不猜。
2. COUNT(*)在sessions局部单表作用域中，契约引用其主键session_id；原规则因为整个SQL另有支付CTE而拒绝。现按COUNT自身作用域核实单个物理表及主键，不以全局表数决定。局部联结、其他来源和非主键不能凭此快捷规则获得认证；这只处理来源对应，不证明最终输出或整个业务口径。

最终原候选成功：SQL生成账本1/3，真实MCP1次，模型0次；查询约0.757秒，开发回放总约1.534秒。使用公开快照CSV、csv.DictReader与Decimal独立核算，8项匹配：

| 月份 | 商品GMV（EUR） | 支付订单数 | 会话数 | 订单客单价（EUR） |
|---|---:|---:|---:|---:|
| 2026-06 | 39,200.44 | 946 | 24,932 | 41.44 |
| 2026-07 | 30,905.83 | 687 | 24,704 | 44.99 |

金额原始单位为欧分，按Europe/Berlin统计，独立核算核对唯一capture与主键。此次首查询未取得商品件数，也没有完成原因下钻；不能据此宣称原用户问题已回答或真实主模型已稳定。

## 测试与尚待验证

固定验收范围见新目录acceptance.md。11项新增顺序／错误集成测试使用受控模型和真实本地MCP；6项提示词示例校验接实际Schema、Skill读取和批次校验；6项作用域／权限测试验证合法CTE与错误拒绝。业务运行与这些控制流程测试分开记录。

首轮600项回归出现两项旧测试错误：权限拒绝已从ValueError正确分类为PermissionError，旧断言尚未同步。改为明确要求PermissionError，不放宽拒绝条件；初次日志保留。最终完整回归600项通过，163.401秒；E阶段14项、前端类型检查与生产构建通过。

Work设置禁止浏览器访问，界面视觉／点击验收未完成。本轮不增加付费试跑；主模型是否在真实任务中更可靠地选择方法、选指标和推进调查，仍待后续实际验收。

历史任务、已花预算和原失败记录不改写，不自动重跑、返还次数或升级成成功。新规则用于后续正常执行；数据与历史保护见验证收据。

记录目录：data/homenova_harness_v1/private/optional_skill_prompt_20261029/。


## 部署与记录

已通过原启动脚本重启后端，8001／5174健康。线上配置接口确认经营ReAct有效正文与新默认提示词逐字一致（4619字符，配置覆盖版本仍为0），加载说明已更新；主模型qwen3.7-plus、入口qwen3.7-flash保持原值。管理员重新登录后，在「Agent配置 → 经营ReAct」点击重新读取即可查看新正文。

134份业务CSV、5份注册Skill正文、12个历史任务及其预算均保持不变。未自动重跑历史失败；单条原SQL回放也不等于完整原因调查已完成。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --record-dir data/homenova_harness_v1/private/optional_skill_prompt_20261029/runtime
```

- [验收清单](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/acceptance.md)、[最终验证收据](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/verification.json)。
- [原SQL成功回放及独立核数](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/replay_first_query_receipt.json)、[首个别名拒绝](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/replay_first_query_initial_receipt.json)、[后续COUNT作用域拒绝](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/replay_first_query_scope_receipt.json)。
- [最终600项回归](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/backend_tests.log)、[首轮两项旧异常断言](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/backend_tests_initial.log)、[作用域与顺序专项](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/targeted_scope_tests.log)。
- [线上提示词确认](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/runtime/prompt_receipt.json)、[服务健康](../data/homenova_harness_v1/private/optional_skill_prompt_20261029/runtime/health.json)。

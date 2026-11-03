# AskData 应用级业务 Skill 来源稿

本目录维护三份应用业务 Skill 的来源正文；运行时使用 `references/askdata_studio/backend/app/skills/` 中显式注册的副本，不扫描 docs 或整个仓库。它们不是安装到个人 Codex 的 Skill，也不能靠 Markdown 路径自动生效。

| Skill | 解决的问题 |
|---|---|
| [business_diagnosis](business_diagnosis/SKILL.md) | 在用户当前范围内核实经营变化、建立相关指标概览，按证据调查解释 |
| [goal_analysis](goal_analysis/SKILL.md) | 对齐目标与实际，计算进度、缺口和有明确假设的条件式情景 |
| [inventory_risk_check](inventory_risk_check/SKILL.md) | 核验可售库存、历史缺货和采购事实，有参数才给条件式风险判断 |

## ReAct 适配规则

- Flash 只做意图路由和目标／范围／已有信息／待答问题理解，不选择业务 Skill。
- ReAct 先看到白名单目录，按需请求读取选中正文，实际返回后才能依据正文决策。已加载且适用的正文复用，不另设选 Skill 模型。
- 业务方法规定调查起点、必要产物、证据边界和充分性，不预排完整步骤，不固定店铺／品类／SKU路线。具体动作根据真实结果改变。
- 用户指定对象时承接该对象；已有看板结果可在范围、期间和口径适用时复用，用户转述与原因判断保留为待核实线索。不能自行制造目标歧义或先预定原因。
- 当前 Schema、指标和真实结果按需取得；默认继承适用资料，只补缺项，按稳定编号和版本去重。公共定义可引用，不能把每次检索包累计塞入上下文；本轮判断需要的数值和必要定义不能省略。
- 步骤后执行代码校验，异常由 Controller 和受控 ReAct 分支处理；无独立过程 Reviewer。正常交付草稿后独立最终审核一次，有必要缺口可继续取证；仅实质变化且必要时再审。
- Skill 不调整权限、快照、工具／时间／费用预算或修复计数，不派生子 Agent，不执行经营操作。

## 公共输入与来源

运行层须实际提供完整问题与适用条件、唯一绑定数据源／快照、当前机器时间及业务时区、相关公开语义条目及依赖、可用工具、调查进度和真实结果。结果保留 ID、SQL／口径、范围、期间、单位、行数、完整性和检查状态。

所有数据源的相对时间统一由机器真实今天解释；快照场景日期只作元信息。没有说明“最近多久”且无可继承期间时先问。已解析期间在当前任务继续和恢复时保持。数据仍受绑定快照和覆盖限制：超出覆盖不当零、不自动改查快照月份、不跨截面补未来。

- 最新框架依据：[ReAct框架调整方案](../ecommerce_agent_react_framework_adjustment.md)。历史架构与验证状态见[设计共识](../ecommerce_agent_design_decisions.md)，旧记录不等于新框架已验证。
- 应用组织参考：[database_query](../../references/askdata_studio/backend/app/skills/database_query/SKILL.md)、[data_qa](../../references/askdata_studio/backend/app/skills/data_qa/SKILL.md) 与 [registry.py](../../references/askdata_studio/backend/app/skills/registry.py)。
- 公开语义候选：[asof_2026_08_31](../../data/homenova_phase_e/public_semantics/asof_2026_08_31/) 和 [asof_2026_10_15](../../data/homenova_phase_e/public_semantics/asof_2026_10_15/)。每次任务只使用授权绑定的一份，不拼接截面。
- metrics.json 提供定义、公式、事件时间、依赖和重复风险；schema.json 提供粒度、表字段与关系；glossary.json 提供歧义；capabilities.json 和 dataset_metadata.json 提供覆盖、观察截点、时区、成熟度和单位。路径只是来源说明，运行时必须实际取得选中内容。

## 仍需业务参数的能力

目标值、指标、周期、范围、单位和来源须来自用户或合法配置；当前无独立目标表时不造目标。库存未来风险仍需期限、需求方法、阈值／服务水平及验收可用假设；不新增复杂预测。比较基准和会改变答案的歧义按上下文或澄清取得，不设未经确认的异常阈值。

## 发布与验证状态

三份来源稿与实际应用副本同步维护，注册器只按既有白名单名称读取。2026-10-27的显式注册脚本已接入旧 Harness；本次正文已按2026-10-29确认的 ReAct、机器当前时间和证据要求修订。正文同步本身不代表新的选择／加载链路或真实业务效果已经验证，实际状态以本次实施报告和运行记录为准。

业务 Skill 只使用公开语义与通用方法，不包含私有题库、参考 SQL、答案、生成参数或评测报告。旧过程报告是开发资料，不能写回运行时方法。

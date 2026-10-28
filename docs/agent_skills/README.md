# AskData 应用级业务 Skill 初稿

本目录保留应用级业务 Skill 来源草稿，不是安装到 `~/.codex/skills` 的个人 Skill。Harness V1 已在实际应用目录显式注册副本，沿用 `registry.py` 的配置加载机制；本目录正文不会被运行时自动加载。注册状态与真实效果分别见文末接入状态和验收报告。

| Skill | 解决的问题 |
|---|---|
| [business_diagnosis](business_diagnosis/SKILL.md) | 核实经营变化，按证据选择方向，区分贡献、关联和原因证明 |
| [goal_analysis](goal_analysis/SKILL.md) | 对齐目标与实际，计算进度/缺口，按需做有假设的达成与弥补分析 |
| [inventory_risk_check](inventory_risk_check/SKILL.md) | 核验可售库存、历史缺货和采购安排，有参数才作条件式风险判断 |

## 公共输入与来源

运行层须按需**实际注入**完整问题与确认条件、唯一绑定数据源/快照、公开语义相关条目及依赖、可用工具、真实结果和结果元信息（来源 SQL/契约、范围、粒度、单位、行数及截断标记）。只引用 Markdown 或 JSON 路径不会加载内容；切换调查方向时也须提供新增语义。旧结果只有快照与口径匹配、覆盖和完整性合格才可复用。

- 流程依据：[已确认设计共识](../ecommerce_agent_design_decisions.md)。其中旧数据进度和待定指标不替代公开语义。
- 应用组织参考：[database_query](../../references/askdata_studio/backend/app/skills/database_query/SKILL.md)、[data_qa](../../references/askdata_studio/backend/app/skills/data_qa/SKILL.md) 与 [registry.py](../../references/askdata_studio/backend/app/skills/registry.py)。
- 公开语义候选：[asof_2026_08_31](../../data/homenova_phase_e/public_semantics/asof_2026_08_31/) 和 [asof_2026_10_15](../../data/homenova_phase_e/public_semantics/asof_2026_10_15/)。运行时只使用绑定的一份，不能拼接。
- 每份快照的 `metrics.json` 提供公式、事件时间、依赖和重复风险；`schema.json` 提供粒度与关系；`glossary.json` 提供歧义；`capabilities.json`、`dataset_metadata.json` 提供能力、场景时间、覆盖、成熟度线索和单位。

## 待确认参数与接入点

- 目标：当前无独立经营目标表；待用户或合法配置提供目标值、指标、周期、范围、单位和来源/版本。
- 库存：当前无现成安全库存阈值或需求预测定义；未来判断还需评估期限、需求方法/窗口、服务水平或阈值规则、采购可用时间与验收假设。
- 诊断/推演：比较基准、必要业务歧义及情景参数按问题确认；不预设异常阈值、根因或必需的固定下钻层级。
- Harness 后续负责业务 Skill 选择、语义/结果注入、查询与分析分工、审核、状态、权限、预算及重试。本轮未实现这些接口。
- 接入时需协调现有查询 Skill 的时间工具要求：本数据的相对时间必须由场景日期解释，不能直接使用机器时间。`database_query` 负责新查询，`data_qa` 只处理已有结果；结果不足由运行层安排补查，不靠 Markdown 引用自动交接。

本文与三个 Skill 仅依据上述允许材料编写；未使用私有题库、参考答案、生成参数或私有手册。静态检查只验证文档与边界，不能代表实际分析效果。


## Harness V1 接入状态（2026-10-27）

三份草稿已由显式发布脚本 `scripts/homenova_harness_v1/register_skills.py` 复制到实际应用的 `backend/app/skills/` 并配置各自的 `config.json`；运行时不读取本目录。入口预处理在原同一次调用中选择业务Skill，注册器按名称读取选中正文，Planner/分析/审核实际注入选中内容。目标与库存保留兼容方法，不代表复杂预测已验证。

接入、控制测试及真实效果分别见 `docs/homenova_harness_v1_report.md`。前述“待接入”描述是草稿交付时的历史状态，不能再用来判断当前注册状态；实际效果以真实运行记录为准。

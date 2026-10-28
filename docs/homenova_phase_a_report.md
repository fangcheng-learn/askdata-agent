# HomeNova A阶段最小交易闭环：实际实施报告

**开发资料，不提供给被评测Agent。** 本文数值由已导出的业务事实及实际校验文件生成；没有以手册目标替代结果。A阶段已运行完成，未实施B阶段、AskData接入、Harness或应用配置修改。

## 1. 范围与实际观察边界

- 单店HN-DE-01、单仓WH-DE-01、EUR，八个SKU使用手册正常价格和固定到仓成本。
- 新访问和下单窗口为德国当地2026-03-01 00:00至2026-03-15 00:00，左闭右开；仅自然搜索与直接访问。
- 公共元数据的UTC窗口：2026-02-28T23:00:00Z 至 2026-03-14T23:00:00Z。
- 观察截至德国当地2026-04-15 00:00，即 2026-04-14T22:00:00Z；截止时点本身不纳入。
- 尾窗只推进既有订单的支付、取消、履约和售后，不新增会话或订单；未来事件继续排队，不强制结清。
- 不启用广告、竞品、促销和其他月份异常；广告关联字段为空，不伪造引用。

## 2. 文件与隔离

| 产物 | 位置 |
|---|---|
| 生成器、固定配置及小样本配置 | [scripts/homenova_phase_a](../scripts/homenova_phase_a) |
| 十张CSV和中性元数据 | [data/homenova_phase_a/public](../data/homenova_phase_a/public) |
| Agent可见字典 | [data_dictionary.md](../data/homenova_phase_a/public/data_dictionary.md) |
| 独立校验程序 | [validate.py](../scripts/homenova_phase_a/validate.py) |
| 主集校验及独立核算指标 | [validation.json](../data/homenova_phase_a/private/validation.json) |
| 分支小样本和校验 | [fixtures](../data/homenova_phase_a/private/fixtures)、[fixture_validation.json](../data/homenova_phase_a/private/fixture_validation.json) |
| 实际验收测试与日志 | [test_results.json](../data/homenova_phase_a/private/test_results.json)、[test_results.txt](../data/homenova_phase_a/private/test_results.txt) |
| 默认种子复现证据 | [reproducibility.json](../data/homenova_phase_a/private/reproducibility.json) |
| 生成配置快照、内部事件日志 | [generation_manifest.json](../data/homenova_phase_a/private/generation_manifest.json)、[events.jsonl](../data/homenova_phase_a/private/events.jsonl) |

未来Agent只应挂载public目录。程序、配置、内部日志、测试样本标签、本报告和原故事手册都属于开发资料。当前只完成文件分区，未配置运行时工具或文件访问权限。

## 3. 实际数据量

| 业务表 | 主集行数 | 独立小样本行数 |
|---|---:|---:|
| sku_catalog | 8 | 8 |
| price_intervals | 8 | 8 |
| sessions | 14015 | 13 |
| orders | 524 | 13 |
| order_items | 655 | 15 |
| payments | 620 | 16 |
| procurements | 3 | 3 |
| inventory_movements | 1334 | 42 |
| shipments | 488 | 11 |
| refunds | 52 | 13 |

主集成功支付订单 **504** 笔，已支付商品销售单位 **708** 个。订单总数与成功支付数不同，失败尝试不增加订单或成交额。小样本成功支付 12 笔，用于覆盖分支，不混入主集经营指标。

## 4. 从业务事实独立核算的关键指标

下表均为观察截点之前的累计事实，单位EUR；收入与成本按照各自事件日确认，没有把两周付款全部视为当天发货。

| 指标 | 实际金额EUR | 口径 |
|---|---:|---|
| 商品GMV | 20898.92 | 成功capture、折后含税、不含运费、退款前 |
| 成功收款现金 | 22893.22 | 含商品与顾客运费、含税 |
| 已收顾客运费 | 1994.30 | 成功支付订单顾客含税运费 |
| 发货确认商品收入 | 16970.00 | 发货日确认、不含税、不含运费 |
| 已发货商品退款冲减 | 685.52 | 完成退款时冲减、不含税；发货前取消不计 |
| 商品净销售额 | 16284.48 | 发货收入减已完成的已发货商品退款 |
| 完成现金退款 | 1580.32 | 已完成商品及运费含税退款，包括发货前取消 |
| 发货销售成本 | 6820.20 | 发货件数乘原成本，不含税 |
| 可再售退货成本恢复 | 182.60 | 验收时恢复原成本，不以退款完成替代 |
| 商品毛利 | 9646.88 | 净销售额减出库成本加验收成本恢复 |
| 现金净流入 | 21312.90 | 成功收款减完成现金退款，不等于净销售额 |
| 阶段内直接经营贡献 | 8370.38 | 商品毛利加净运费收入，减支付/承运/退运处理费用；不含未启用广告与固定费用 |

对账桥：商品GMV 20898.92 + 已收运费 1994.30 = 成功收款 22893.22。

商品毛利桥：16284.48 − 6820.20 + 182.60 = 9646.88。

## 5. 分支覆盖与开放状态

以下从实际十表判定，不读取隐藏标签。数量是满足条件的订单或退款分摊行，可重叠，不相加当作订单总量。

| 分支 | 主集观察数 | 小样本观察数 |
|---|---:|---:|
| 发货且无退款申请订单 | 454 | 2 |
| 失败后重试成功 | 56 | 1 |
| 支付超时 | 20 | 1 |
| 已支付未发货取消 | 16 | 1 |
| 退部分件数的退款行 | 6 | 4 |
| 已完成仅退款 | 8 | 4 |
| 已验收可再售退货 | 14 | 2 |
| 退款早于验收 | 8 | 2 |
| 验收早于退款 | 4 | 1 |
| 仍在申请或审核中的退款行 | 6 | 1 |
| 已退款但未验收退货 | 0 | 1 |
| 被拒绝退款 | 5 | 1 |
| 尚未验收采购批次 | 1 | 1 |

主集尚有 6 行开放退款，未完成资金退款的税额与不含税实退额为空，不计入完成退款或收入冲减。未验收采购不增加库存。私有队列尚有 21 个未来事件，保留开放状态符合截点规则。

## 6. 校验与验收结果

独立校验器只读取十张公共CSV与中性元数据，不导入生成器、配置或内部日志，不采用生成器汇总自证。逐记录执行以下检查组，12是分组数，不是只检查12条记录。

| 检查组 | 主集 | 小样本 |
|---|---|---|
| files_schema_metadata | 通过 | 通过 |
| types_utc_and_observation_cutoff | 通过 | 通过 |
| primary_foreign_business_keys | 通过 | 通过 |
| manual_catalog_and_baseline_prices | 通过 | 通过 |
| natural_sessions_window_and_ads_disabled | 通过 | 通过 |
| order_items_tax_shipping_and_head_totals | 通过 | 通过 |
| payment_retries_capture_and_order_lifecycle | 通过 | 通过 |
| shipment_evidence_and_order_status | 通过 | 通过 |
| procurement_acceptance_and_open_quantities | 通过 | 通过 |
| refund_caps_tax_states_and_cost_recovery | 通过 | 通过 |
| every_inventory_prefix_and_source_documents | 通过 | 通过 |
| independent_metrics_and_daily_reconciliations | 通过 | 通过 |

- 实际验收测试：17 项全部通过，无失败、错误或跳过；详细逐项结果见私有JSON和日志。
- 8类故意损坏：金额加1欧分、孤儿SKU、重复主键、负库存、未来退款、重复成功扣款、失败支付收费、重复库存序号；校验器均拒绝。去掉行数元数据后，重复主键与金额错误仍被独立业务规则发现。
- 固定默认配置重跑：十张业务CSV逐字节一致，内部事件日志亦一致；另一个随机种子会改变业务事实。
- as-of测试覆盖退款尚未完成、资金已退但未验收、会话正在进行；不提前填终态或恢复成本。
- 独立微型时区测试验证跨夏令时的71小时UTC窗口与等价截点；该临时测试不属于正式两周主集，正式校验器仍锁定本次A阶段日期。
- 56 个受保护文件的前后SHA-256全部一致，包括旧电商表、原手册和已有改动文件；当前任务没有修改它们。

实施中统一了两项边界：支付超时使用expires_at，主动取消才填canceled_at；正在进行的会话ended_at可以为空，校验器不能把未知结束误判为坏数据。两者已修正并重新运行验证，没有遗留失败项。

## 7. 复现命令

在项目根目录使用Python 3.9及以上版本；只使用标准库，无需新增依赖。生成器只覆盖带有本程序生成清单的目录，拒绝覆盖未知非空目录。

```sh
python3 scripts/homenova_phase_a/generate.py --config scripts/homenova_phase_a/config.json --output data/homenova_phase_a
python3 scripts/homenova_phase_a/validate.py --data data/homenova_phase_a/public --output data/homenova_phase_a/private/validation.json
python3 scripts/homenova_phase_a/generate.py --fixture-suite --output data/homenova_phase_a/private/fixtures --as-of 2026-04-16T00:00:00+02:00
python3 scripts/homenova_phase_a/validate.py --data data/homenova_phase_a/private/fixtures/public --output data/homenova_phase_a/private/fixture_validation.json
python3 -m unittest discover -s scripts/homenova_phase_a -p test_phase_a.py -v
python3 scripts/homenova_phase_a/build_report.py
```

测试在临时目录构造重跑与损坏副本，不破坏主数据。观察截点可通过--as-of覆盖；小样本与自然主集应使用不同输出目录。业务CSV、日志及清单可复现；测试耗时是实际运行值，不要求每次相同。报告脚本读取已落盘实际结果，不执行生成或伪造测试状态。

## 8. 可以进入B阶段吗

**可以进入B阶段的设计与实现。** A阶段已证明两周范围内的访问、交易、支付、库存、采购、履约与售后可生成、可复现且可对账。当前未验证广告归因、竞品影响、站内行为漏斗、六个月库存与经营故事；不将A阶段通过等同这些能力已完成。

A阶段采用预先下达的三个采购批次来验证到仓与验收的区别，没有启用完整六个月自动补货策略、批次成本变化、拆包或预售。进入B时先扩相互关联的广告/访问行为和竞品事实，再校准完整故事；保留现有校验作为回归门槛。

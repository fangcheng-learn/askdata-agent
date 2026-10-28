#!/usr/bin/env python3
"""Structure neutral HomeNova semantics from public dictionaries and CSV headers.

Only public snapshot metadata, the neutral public dictionary, and CSV headers
are interpreted. CSV bytes may be hashed for provenance, but values are not
profiled or used to infer business explanations. No embeddings are produced.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_BINDINGS = {
    "asof_2026_08_31": ("homenova_20260831", "hn08"),
    "asof_2026_10_15": ("homenova_20261015", "hn10"),
}
TABLE_NAMES = (
    "sku_catalog", "price_intervals", "sessions", "orders", "order_items", "payments",
    "procurements", "inventory_movements", "shipments", "refunds", "campaigns", "ad_daily",
    "visit_events", "competitor_prices", "customer_feedback",
)
PRIMARY_KEYS = {
    name: [field] for name, field in zip(TABLE_NAMES, (
        "sku_id", "price_interval_id", "session_id", "order_id", "order_item_id", "payment_id",
        "procurement_id", "movement_id", "shipment_id", "refund_line_id", "campaign_id",
        "campaign_id", "event_id", "observation_id", "feedback_event_id"))
}
PRIMARY_KEYS["ad_daily"] = ["campaign_id", "stat_local_date"]
UNIQUE_KEYS = {"orders": [["checkout_session_id"]], "order_items": [["order_id", "sku_id"]],
               "shipments": [["order_id"]]}
DOMAINS = {
    "sku_catalog": "商品", "price_intervals": "商品", "sessions": "访问", "orders": "交易",
    "order_items": "交易", "payments": "资金", "procurements": "采购", "inventory_movements": "库存",
    "shipments": "履约", "refunds": "售后与资金", "campaigns": "广告", "ad_daily": "广告",
    "visit_events": "访问与广告", "competitor_prices": "市场观测", "customer_feedback": "客户服务与评价",
}
TABLE_DEPENDENCIES = {
    "sku_catalog": "sku_id category_code pack_units currency standard_cost_ex_vat_cents vat_rate_bps active_from active_to recorded_at",
    "price_intervals": "price_interval_id sku_id valid_from valid_to published_at recorded_at",
    "sessions": "session_id visitor_id identity_linkable started_at ended_at source_channel source_click_event_id landing_sku_id is_internal_traffic recorded_at",
    "orders": "order_id visitor_id checkout_session_id ordered_at expires_at canceled_at cancel_recorded_at status_as_of attributed_click_event_id attributed_campaign_id attribution_recorded_at recorded_at",
    "order_items": "order_item_id order_id sku_id price_interval_id quantity recorded_at",
    "payments": "payment_id order_id status attempted_at captured_at capture_recorded_at recorded_at",
    "procurements": "procurement_id sku_id warehouse_id ordered_at expected_receipt_at actual_arrival_at arrival_recorded_at accepted_at acceptance_recorded_at status_as_of recorded_at",
    "inventory_movements": "movement_id event_seq sku_id warehouse_id movement_type occurred_at recorded_at on_hand_delta reserved_delta order_item_id procurement_id refund_line_id shipment_id",
    "shipments": "shipment_id order_id warehouse_id shipped_at promised_delivery_at delivered_at delivery_recorded_at status_as_of recorded_at",
    "refunds": "refund_line_id refund_id order_item_id payment_id refund_kind status_as_of requested_at approved_at approval_recorded_at completed_at completion_recorded_at returned_at return_recorded_at inspected_at inspection_recorded_at return_cost_incurred_at return_cost_recorded_at recorded_at",
    "campaigns": "campaign_id campaign_family_id target_sku_id channel_code targeting_type valid_from valid_to action_recorded_at currency",
    "ad_daily": "campaign_id stat_local_date budget_cap_ex_vat_cents budget_action_at impressions clicks spend_ex_vat_cents report_complete recorded_at",
    "visit_events": "event_id event_seq event_type occurred_at recorded_at session_id visitor_id campaign_id sku_id available_qty_observed failure_reason",
    "competitor_prices": "observation_id mapped_sku_id observed_at recorded_at collection_status comparability_grade in_stock_observed",
    "customer_feedback": "feedback_event_id parent_event_id order_id order_item_id event_type occurred_at recorded_at issue_code status_code rating",
}
ENUMS = {
    "orders.status_as_of": ["pending_payment", "paid", "expired", "cancelled", "shipped", "delivered"],
    "payments.status": ["failed", "captured"],
    "procurements.status_as_of": ["ordered", "arrived", "accepted"],
    "shipments.status_as_of": ["in_transit", "delivered"],
    "refunds.status_as_of": ["requested", "approved", "completed", "rejected"],
    "refunds.refund_kind": ["pre_ship_cancel", "return_and_refund", "refund_only", "shipping_only"],
    "visit_events.event_type": ["ad_click", "product_view", "add_to_cart", "checkout_attempt", "inventory_blocked", "substitute_click"],
    "customer_feedback.event_type": ["complaint_opened", "complaint_replied", "complaint_resolved", "review_submitted"],
    "customer_feedback.status_code": ["open", "in_progress", "resolved", "published"],
}

# These are logical relations transcribed from the public key/field definitions.
# Cardinality is declared, not estimated from this particular data population.
RELATIONS = [
    ("price_sku", "sku_catalog", "sku_id", "price_intervals", "sku_id", "1:N", False, "价格区间按SKU与有效时间唯一匹配；优先用订单行price_interval_id关联。"),
    ("campaign_sku", "sku_catalog", "sku_id", "campaigns", "target_sku_id", "1:N", False, "计划目标SKU与购物篮SKU可以不同。"),
    ("ad_campaign", "campaigns", "campaign_id", "ad_daily", "campaign_id", "1:N", False, "广告日记录联合主键为campaign_id与stat_local_date。"),
    ("events_campaign", "campaigns", "campaign_id", "visit_events", "campaign_id", "1:N", True, "仅ad_click填写campaign_id；非点击行为不作广告计划归因。"),
    ("events_session", "sessions", "session_id", "visit_events", "session_id", "1:N", True, "未落地ad_click允许session_id为空；站内事件必须有会话。"),
    ("session_source_click", "visit_events", "event_id", "sessions", "source_click_event_id", "1:0..1", True, "来源点击必须event_type=ad_click且不晚于会话开始；非付费会话为空。"),
    ("events_sku", "sku_catalog", "sku_id", "visit_events", "sku_id", "1:N", True, "行为适用的观察/目标SKU，不等于一笔已成交订单。"),
    ("orders_session", "sessions", "session_id", "orders", "checkout_session_id", "1:0..1", False, "checkout_session_id在订单中唯一，按该会话计算购买转化。"),
    ("orders_attributed_click", "visit_events", "event_id", "orders", "attributed_click_event_id", "1:N", True, "同可连接访客，点击不晚于下单且距capture不超过7×24小时，整单至多归一次。"),
    ("orders_attributed_campaign", "campaigns", "campaign_id", "orders", "attributed_campaign_id", "1:N", True, "冗余归因计划必须等于归因点击的计划；自然回访不覆盖有效付费点击。"),
    ("order_items_order", "orders", "order_id", "order_items", "order_id", "1:N", False, "订单头金额连接明细后不可逐行重复求和。"),
    ("sku_order_items", "sku_catalog", "sku_id", "order_items", "sku_id", "1:N", False, "同订单同SKU一行；套装数量按销售单位计。"),
    ("items_price", "price_intervals", "price_interval_id", "order_items", "price_interval_id", "1:N", False, "价格在下单时锁定；SKU与下单时间须符合价格区间。"),
    ("payments_order", "orders", "order_id", "payments", "order_id", "1:N", False, "支付尝试可多次，成功captured至多一次；失败不计金额和GMV。"),
    ("procurement_sku", "sku_catalog", "sku_id", "procurements", "sku_id", "1:N", False, "purchase_reference不是唯一批次键，每procurement_id单次验收。"),
    ("sku_inventory", "sku_catalog", "sku_id", "inventory_movements", "sku_id", "1:N", False, "库存按SKU、仓、occurred_at与event_seq累计，不能跨天加库存余额。"),
    ("movement_order_item", "order_items", "order_item_id", "inventory_movements", "order_item_id", "1:N", True, "reserve/release/ship引用订单行；支付不再次扣库。"),
    ("procurement_inventory", "procurements", "procurement_id", "inventory_movements", "procurement_id", "1:0..1", True, "仅receipt引用采购批次，验收数量与入库量一致。"),
    ("shipments_order", "orders", "order_id", "shipments", "order_id", "1:0..1", False, "全订单一次发货；发货才确认收入及COGS。"),
    ("shipment_inventory", "shipments", "shipment_id", "inventory_movements", "shipment_id", "1:N", True, "ship逐商品行扣在库与预占，发货单费用不可重复到各库存行。"),
    ("refunds_item", "order_items", "order_item_id", "refunds", "order_item_id", "1:N", False, "多次部分退款按原明细限额；refund_id可有多行，不能重复整笔运费。"),
    ("refunds_payment", "payments", "payment_id", "refunds", "payment_id", "1:N", False, "退款引用成功captured支付，不写负支付重复冲减。"),
    ("refund_inventory", "refunds", "refund_line_id", "inventory_movements", "refund_line_id", "1:0..1", True, "仅实际退回验收可再售产生return_restock，资金完成不自动补库。"),
    ("competitor_sku", "sku_catalog", "sku_id", "competitor_prices", "mapped_sku_id", "1:N", False, "仅用当时已知、未过期、规格包装可比的观测；failed价格为空。"),
    ("feedback_order", "orders", "order_id", "customer_feedback", "order_id", "1:N", False, "根咨询/投诉、回复、解决、评价多行不可放大订单或支付金额。"),
    ("feedback_item", "order_items", "order_item_id", "customer_feedback", "order_item_id", "1:N", True, "订单级反馈商品键为空，不强行分配到所有SKU；商品行必须属于相同订单。"),
    ("feedback_parent", "customer_feedback", "feedback_event_id", "customer_feedback", "parent_event_id", "1:N", True, "回复与解决直接引用complaint_opened根，并继承order/item/issue；非根跟进不新增投诉。"),
]


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def parse_dictionary(text):
    sections = {}
    pattern = r"^### 3\.\d+ `([a-z_]+)`：([^\n]+)\n(.*?)(?=^### 3\.|^## 4\.|\Z)"
    for match in re.finditer(pattern, text, re.M | re.S):
        table, label, body = match.groups()
        fields = []
        for row in re.finditer(r"^\| `([a-z_]+)` \| ([^|]+) \| ([^|]+) \|\s*$", body, re.M):
            name, type_text, description = [part.strip() for part in row.groups()]
            fields.append({"name": name, "type_text": type_text, "description": description})
        intro = body.split("| 字段", 1)[0].strip()
        sections[table] = {"label": label.strip(), "description": intro, "fields": fields}
    grains = {}
    for line in text.splitlines():
        match = re.match(r"\| `([a-z_]+)` \| ([^|]+) \|", line)
        if match and match.group(1) in TABLE_NAMES and match.group(1) not in grains:
            grains[match.group(1)] = match.group(2).strip()
    if set(sections) != set(TABLE_NAMES) or sum(len(x["fields"]) for x in sections.values()) != 207:
        raise ValueError("Public neutral dictionary must define fifteen tables and 207 fields")
    return sections, grains


def sql_type(type_text):
    if "UTC_TIMESTAMP" in type_text:
        return "TIMESTAMPTZ", "日期"
    if "INTEGER" in type_text:
        return "BIGINT", "整数"
    if "BOOLEAN" in type_text:
        return "BOOLEAN", "布尔"
    if "DATE" in type_text:
        return "DATE", "日期"
    return "VARCHAR", "文本"


def field_label(description, fallback):
    # Neutral names from the public field contract, not synthetic event labels.
    labels = {
        "category_code": "商品类目代码", "pack_units": "包装内物理件数",
        "currency": "交易币种", "vat_rate_bps": "增值税率基点",
        "status": "支付尝试状态", "status_as_of": "观察截点业务状态",
        "recorded_at": "首次记录时间", "source_channel": "会话来源渠道",
        "targeting_type": "广告定向类型", "landing_sku_id": "落地商品SKU",
        "destination_postal_prefix": "收货邮区前两位", "postal_prefix": "配送邮区前两位",
        "visitor_id": "访客伪匿名标识", "identity_linkable": "访客标识可连接",
        "promotion_code": "促销标识", "device_type": "访问设备类型",
        "source_click_event_id": "来源广告点击事件", "click_cost_ex_vat_cents": "单次点击费用不含税欧分",
    }
    if fallback in labels:
        return labels[fallback]
    value = re.sub(r"^(非空正整数|非空非负|非空|可空)[；，]?", "", description)
    value = value.split("；")[0].strip()
    return value[:32] or fallback


def metrics_catalog():
    metrics = []
    def add(ident, label, aliases, formula, fields, joins=(), deps=(), time_fields=(),
            predicates=(), grain="店铺×当地经营日，可按事实支持的SKU/渠道向上汇总", unit="EUR_cents_ex_vat",
            tax="ex_vat", shipping="excluded", refund="依指标定义", maturity="截点前已发生且已知", risks=()):
        metrics.append({"id":ident,"label":label,"aliases":aliases,"definition":formula,"formula":formula,
                        "direct_fields":fields.split(),"join_keys":list(joins),"depends_on":list(deps),
                        "time_fields":[{"table":x.split('.')[0],"field":x.split('.')[1]} for x in time_fields],
                        "required_predicates":list(predicates),"grain":grain,"unit":unit,"tax_policy":tax,
                        "shipping_policy":shipping,"refund_policy":refund,"maturity":maturity,
                        "duplicate_risks":list(risks),"as_of_policy":"业务发生和信息可得时间均严格早于当前截点；只读当前绑定快照。"})
    add("paid_goods_gmv_gross","成功支付商品GMV",["GMV","商品GMV","成交额","支付商品销售额"],
        "存在唯一成功capture订单的商品行折后含税额之和；后续退款不回写历史GMV。",
        "payments.payment_id payments.order_id payments.status payments.captured_at payments.capture_recorded_at order_items.order_id order_items.sku_id order_items.line_paid_gross_cents",
        ["payments_order","order_items_order"],time_fields=["payments.captured_at"],predicates=["payments.status='captured'"],
        unit="EUR_cents_gross",tax="gross",refund="before_refunds",risks=["先按订单聚合或确保每单仅一个成功capture，失败尝试不可重复联结明细。"])
    add("paid_order_count","成功支付订单量",["支付订单数","成交单量"],"成功captured对应不同order_id计数，退款后仍保留历史成交。",
        "payments.order_id payments.status payments.captured_at payments.capture_recorded_at",time_fields=["payments.captured_at"],
        predicates=["payments.status='captured'"],unit="orders",tax="not_applicable",refund="退款不倒扣",risks=["不同SKU的订单数不可直接相加。"])
    add("goods_aov_gross","商品客单价",["客单价","支付客单价"],"相同支付订单集合的成功支付商品GMV/成功支付订单量，零分母为NULL。","",
        deps=["paid_goods_gmv_gross","paid_order_count"],unit="EUR_cents_gross_per_order",tax="gross",refund="before_refunds")
    add("shipped_goods_revenue_ex_vat","发货商品收入",["发货销售额","确认商品收入"],"存在发货事实的商品行不含税金额按发货时间加总，未发货不确认收入。",
        "shipments.shipment_id shipments.order_id shipments.shipped_at shipments.recorded_at order_items.order_id order_items.line_ex_vat_cents",
        ["shipments_order","order_items_order"],time_fields=["shipments.shipped_at"],predicates=["shipments.shipped_at IS NOT NULL"],refund="before_refunds")
    add("completed_cash_refunds_gross","完成现金退款",["已退款金额","完成退款金额","现金退回"],"已完成退款分摊的商品含税额+已分摊顾客含税运费；按资金完成时点入期。",
        "refunds.refund_line_id refunds.refund_id refunds.status_as_of refunds.completed_at refunds.completion_recorded_at refunds.item_refund_gross_cents refunds.shipping_refund_gross_cents",
        time_fields=["refunds.completed_at"],predicates=["refunds.completed_at IS NOT NULL"],unit="EUR_cents_gross",tax="gross",shipping="included_as_separate_component",refund="completed_only",risks=["refund_id非唯一；不得把整笔运费重复放到申请各行。"])
    add("shipped_goods_refunds_ex_vat","已发货商品退款冲减",["商品收入冲减","已发货商品退款"],"已发货订单完成的商品不含税退款；排除发货前取消及顾客运费。",
        "refunds.order_item_id refunds.refund_kind refunds.completed_at refunds.completion_recorded_at refunds.item_refund_ex_vat_cents order_items.order_item_id order_items.order_id shipments.order_id shipments.shipped_at",
        ["refunds_item","order_items_order","shipments_order"],time_fields=["refunds.completed_at"],predicates=["refunds.completed_at IS NOT NULL","refunds.refund_kind<>'pre_ship_cancel'","shipments.shipped_at IS NOT NULL"],refund="completed_goods_only")
    add("net_sales_ex_vat","商品净销售额",["净销售","净销售额","扣退商品收入"],"发货商品不含税收入-已发货商品完成退款不含税额；发生期分别按发货和退款完成日。","",
        deps=["shipped_goods_revenue_ex_vat","shipped_goods_refunds_ex_vat"],refund="completed_post_shipment_goods_only")
    add("shipped_goods_cogs_ex_vat","发货商品销售成本",["COGS","销售成本","商品成本"],"存在发货事实的商品行原成本快照加总；采购支出和未发货快照不直接计COGS。",
        "shipments.order_id shipments.shipped_at shipments.recorded_at order_items.order_id order_items.quantity order_items.unit_cost_ex_vat_cents order_items.line_cost_ex_vat_cents",
        ["shipments_order","order_items_order"],time_fields=["shipments.shipped_at"],refund="退款本身不冲成本")
    add("resalable_cost_recovery_ex_vat","退货成本恢复",["成本反冲","可再售成本恢复"],"已验收可再售数量×原订单单位成本；按验收时点，资金退款完成不是必要条件。",
        "refunds.refund_line_id refunds.order_item_id refunds.refund_kind refunds.inspected_at refunds.inspection_recorded_at refunds.returned_qty refunds.restock_qty refunds.recovered_cost_ex_vat_cents order_items.order_item_id order_items.unit_cost_ex_vat_cents",
        ["refunds_item"],time_fields=["refunds.inspected_at"],predicates=["refunds.inspected_at IS NOT NULL","refunds.refund_kind='return_and_refund'"],refund="refund_only/未退回/不可再售不恢复成本")
    add("goods_profit_ex_vat","商品毛利润",["商品毛利","毛利润"],"商品净销售额-发货COGS+退货成本恢复，不含顾客运费和直接经营费用。","",
        deps=["net_sales_ex_vat","shipped_goods_cogs_ex_vat","resalable_cost_recovery_ex_vat"],refund="收入冲减与成本恢复各按实际时点")
    add("goods_margin","商品毛利率",["毛利率"],"商品毛利润/商品净销售额；净销售额<=0时NULL并说明金额，不平均日/SKU比率。","",
        deps=["goods_profit_ex_vat","net_sales_ex_vat"],unit="ratio",refund="after_defined_refunds_and_recovery")
    add("captured_cash_gross","成功实收现金",["成功收款","含税实收"],"captured支付的captured_gross_cents求和，包含顾客运费及税。",
        "payments.payment_id payments.order_id payments.status payments.captured_at payments.capture_recorded_at payments.captured_gross_cents",
        time_fields=["payments.captured_at"],predicates=["payments.status='captured'"],unit="EUR_cents_gross",tax="gross",shipping="included",refund="before_refunds")
    add("net_cash_inflow","现金净流入",["收退款净额"],"成功实收现金-完成现金退款，各按自身发生日；不等于净销售额或利润。","",
        deps=["captured_cash_gross","completed_cash_refunds_gross"],unit="EUR_cents_gross",tax="gross",shipping="included",refund="completed_cash_refunds")
    stock_fields="inventory_movements.movement_id inventory_movements.sku_id inventory_movements.warehouse_id inventory_movements.movement_type inventory_movements.occurred_at inventory_movements.recorded_at inventory_movements.event_seq inventory_movements.on_hand_delta inventory_movements.reserved_delta"
    for ident,label,aliases,formula in [
        ("on_hand_inventory","实物在库量",["在库库存"],"截至时点累计on_hand_delta；残次退货不回销售在库。"),
        ("reserved_inventory","预占库存",["库存预留"],"截至时点累计reserved_delta；取消/超时/发货释放，不在支付时再扣库。"),
        ("available_inventory","可售库存",["可用库存","库存可售数量"],"截至时点累计on_hand_delta-累计reserved_delta，按SKU仓库、occurred_at与event_seq滚动。"),
        ("stockout_duration","缺货时长",["不可售时长","断货区间"],"重建可售为零的实际状态区间，切分经营日后累计秒数/小时；连续区间与累计时长分开。")]:
        add(ident,label,aliases,formula,stock_fields,grain="SKU×仓库×时点；时长需明确观察区间",unit="hours" if ident=="stockout_duration" else "sales_units",tax="not_applicable",time_fields=["inventory_movements.occurred_at"],refund="只有实际可再售验收入库",risks=["库存余额不可跨天求和；在途采购不算可售。"])
    add("sessions_count","访问会话数",["访问量","会话量"],"按会话开始经营日计不同session_id，排除内部流量；不是用户、点击或事件数。",
        "sessions.session_id sessions.started_at sessions.ended_at sessions.is_internal_traffic sessions.source_channel sessions.recorded_at",
        time_fields=["sessions.started_at"],predicates=["sessions.is_internal_traffic=false"],unit="sessions",tax="not_applicable")
    add("checkout_session_cvr","会话购买转化率",["会话转化率","购买转化率"],"同一有效会话集合中通过checkout_session形成成功支付订单的不同会话数/有效会话数；与广告归因窗分开。",
        "sessions.session_id sessions.started_at sessions.ended_at sessions.is_internal_traffic orders.checkout_session_id orders.order_id orders.ordered_at orders.expires_at payments.order_id payments.status payments.captured_at payments.capture_recorded_at",
        ["orders_session","payments_order"],deps=["sessions_count"],time_fields=["sessions.started_at"],predicates=["sessions.is_internal_traffic=false","payments.status='captured'"],unit="ratio",tax="not_applicable",maturity="会话已经结束且其订单30分钟支付窗已足够观察，分子/分母同一会话cohort。")
    add("sku_view_session_cvr","商品浏览会话购买率",["商品转化率","浏览到购买转化"],"浏览某SKU且同checkout_session成功购买该SKU的不同会话数/浏览该SKU的不同有效会话数。",
        "visit_events.event_type visit_events.sku_id visit_events.session_id visit_events.occurred_at sessions.session_id sessions.started_at sessions.is_internal_traffic orders.checkout_session_id orders.order_id orders.expires_at payments.order_id payments.status payments.captured_at order_items.order_id order_items.sku_id",
        ["events_session","orders_session","payments_order","order_items_order"],time_fields=["sessions.started_at"],predicates=["visit_events.event_type='product_view'","sessions.is_internal_traffic=false","payments.status='captured'"],grain="SKU×访问会话cohort",unit="ratio",tax="not_applicable",maturity="会话及其订单支付有效期已足够观察。",risks=["同会话同SKU多次浏览只计一次；不同SKU会话数不一定可加。"])
    add("ad_spend_ex_vat","广告实际消耗",["广告成本","广告花费","媒体费"],"完整ad_daily不含税消耗合计，应与相同campaign及当地点击日的计费点击费用和一致；预算不是消耗。",
        "ad_daily.campaign_id ad_daily.stat_local_date ad_daily.budget_cap_ex_vat_cents ad_daily.spend_ex_vat_cents ad_daily.report_complete ad_daily.recorded_at visit_events.event_type visit_events.campaign_id visit_events.occurred_at visit_events.click_cost_ex_vat_cents",
        ["ad_campaign","events_campaign"],time_fields=["ad_daily.stat_local_date","visit_events.occurred_at"],predicates=["ad_daily.report_complete=true","visit_events.event_type='ad_click'"],refund="媒体费不随订单退款自动消失",maturity="日结是否已上报以当前metadata及recorded_at为准；末日点击可见不代表日结已知。",risks=["广告日总额不可直接连接每个点击后重复求和；分别聚合到campaign×当地日。"])
    add("ad_ctr","广告点击率",["CTR"],"同campaign日的clicks/impressions，汇总分子分母重算，零分母NULL。",
        "ad_daily.campaign_id ad_daily.stat_local_date ad_daily.clicks ad_daily.impressions ad_daily.report_complete ad_daily.recorded_at",unit="ratio",tax="not_applicable",time_fields=["ad_daily.stat_local_date"])
    add("ad_cpc_ex_vat","广告平均点击费",["CPC"],"同campaign日的不含税实际费用/计费点击数，零分母NULL。",
        "ad_daily.campaign_id ad_daily.stat_local_date ad_daily.clicks ad_daily.spend_ex_vat_cents ad_daily.report_complete ad_daily.recorded_at",deps=["ad_spend_ex_vat"],unit="EUR_cents_ex_vat_per_click",time_fields=["ad_daily.stat_local_date"])
    add("ad_landing_rate","广告点击落地率",["点击落地率"],"有对应付费落地会话的点击数/计费点击数；未成功落地点击仍可计费。",
        "sessions.session_id sessions.source_click_event_id sessions.started_at sessions.is_internal_traffic visit_events.event_id visit_events.event_type visit_events.campaign_id visit_events.occurred_at",
        ["session_source_click"],time_fields=["visit_events.occurred_at"],predicates=["visit_events.event_type='ad_click'","sessions.is_internal_traffic=false"],unit="ratio",tax="not_applicable",risks=["点击和会话不是一一全覆盖；不能按会话开始日混配另一批点击日。"])
    add("attributed_goods_sales_ex_vat","广告归因商品成交额",["归因商品收入","广告归因成交"],"成功capture订单按下单前同可连接访客7×24小时内末次付费点击唯一归因，整单折后商品不含税额一次计入。",
        "orders.order_id orders.visitor_id orders.checkout_session_id orders.ordered_at orders.attributed_click_event_id orders.attributed_campaign_id orders.attribution_recorded_at sessions.session_id sessions.identity_linkable payments.order_id payments.status payments.captured_at payments.capture_recorded_at order_items.order_id order_items.line_ex_vat_cents visit_events.event_id visit_events.visitor_id visit_events.event_type visit_events.campaign_id visit_events.occurred_at visit_events.recorded_at visit_events.event_seq campaigns.campaign_id campaigns.channel_code",
        ["orders_session","payments_order","order_items_order","orders_attributed_click","orders_attributed_campaign"],
        time_fields=["visit_events.occurred_at","payments.captured_at"],predicates=["payments.status='captured'","sessions.identity_linkable=true","visit_events.event_type='ad_click'","visit_events.occurred_at<=orders.ordered_at","payments.captured_at-visit_events.occurred_at<=7×24小时","取最后合格点击，每订单一次"],
        grain="点击当地日cohort×campaign/渠道；支付日归因收入必须另命名",refund="before_refunds",maturity="完整7日购买机会；经营停止前七日点击即使尾窗已过仍存在右截断。",risks=["自然checkout可归因于此前付费点击；归因成交不等于广告增量成交。"])
    add("goods_roas_ex_vat","商品成交ROAS",["广告ROAS","成交ROAS","ROAS"],"同点击日期cohort的退款前归因商品不含税成交额/同批点击不含税媒体费；零费用NULL。","",
        deps=["attributed_goods_sales_ex_vat","ad_spend_ex_vat"],unit="ratio",refund="before_refunds",maturity="完整7日购买机会；报告日期与经营停止造成的右截断，最近七日单列。",risks=["不能用支付日收入除同日消耗冒充点击cohortROAS。"])
    add("refund_adjusted_roas","退款后商品ROAS",["扣退ROAS"],"原点击cohort归因商品成交额扣截至as_of已完成商品不含税退款，再除原点击cohort媒体费；包括发货前取消商品退款。",
        "refunds.order_item_id refunds.completed_at refunds.completion_recorded_at refunds.item_refund_ex_vat_cents order_items.order_item_id order_items.order_id",["refunds_item"],deps=["goods_roas_ex_vat"],unit="ratio",refund="completed_goods_in_original_click_cohort",maturity="除点击归因窗，还须声明退款观察截止，不能把退款当月直接减当月广告收入。")
    add("net_customer_shipping_ex_vat","顾客净运费收入",["净运费收入"],"发货确认的顾客不含税运费-已发货订单完成的不含税运费退款；发货前取消不冲未确认收入。",
        "orders.order_id orders.shipping_ex_vat_cents shipments.order_id shipments.shipped_at refunds.order_item_id refunds.refund_kind refunds.completed_at refunds.shipping_refund_ex_vat_cents order_items.order_item_id order_items.order_id",
        ["shipments_order","refunds_item","order_items_order"],time_fields=["shipments.shipped_at","refunds.completed_at"],shipping="included",refund="completed_post_shipment_shipping_only")
    add("direct_operating_contribution_ex_vat","直接经营贡献",["贡献利润","扣广告后贡献"],"商品毛利+顾客净运费收入-广告不含税费-支付费-发货承运费-已发生退运处理费；不含工资租金等固定费用。",
        "payments.payment_fee_ex_vat_cents payments.status payments.captured_at shipments.carrier_cost_ex_vat_cents shipments.shipped_at refunds.return_carrier_cost_ex_vat_cents refunds.handling_cost_ex_vat_cents refunds.return_cost_incurred_at refunds.return_cost_recorded_at",
        deps=["goods_profit_ex_vat","net_customer_shipping_ex_vat","ad_spend_ex_vat"],time_fields=["payments.captured_at","shipments.shipped_at","refunds.return_cost_incurred_at"],shipping="net_customer_shipping_included;carrier_cost_deducted",refund="各实际事件归期",risks=["默认店铺发生期；细分须声明管理分摊，广告费归计划目标SKU；同一费用只扣一次。"])
    add("overdue_order_rate","已到承诺期限订单超期率",["物流延迟率","超期率"],"截至截点已到承诺时间的发货单中，已晚到或仍未送达的不同订单数/该已到期发货订单数。",
        "shipments.shipment_id shipments.order_id shipments.shipped_at shipments.promised_delivery_at shipments.delivered_at shipments.delivery_recorded_at shipments.carrier_code shipments.postal_prefix",
        time_fields=["shipments.shipped_at","shipments.promised_delivery_at","shipments.delivered_at"],grain="发货cohort×承运商/邮区",unit="ratio",tax="not_applicable",predicates=["shipments.promised_delivery_at<当前截点"],maturity="未送达需保留，不仅选已送达样本。")
    add("delivery_duration","已完成配送时长",["配送时长","送达耗时"],"对已送达样本计算delivered_at-shipped_at的中位数/分位数；未送达数量与占比另列。",
        "shipments.order_id shipments.shipped_at shipments.delivered_at shipments.delivery_recorded_at",time_fields=["shipments.shipped_at","shipments.delivered_at"],predicates=["shipments.delivered_at IS NOT NULL"],unit="hours_or_days_explicit",tax="not_applicable",maturity="是已完成样本分布，不把空送达当零时长。")
    feedback_base="customer_feedback.feedback_event_id customer_feedback.parent_event_id customer_feedback.order_id customer_feedback.order_item_id customer_feedback.event_type customer_feedback.occurred_at customer_feedback.recorded_at customer_feedback.issue_code customer_feedback.status_code"
    add("customer_contact_root_count","客户咨询/投诉根数",["咨询数量","投诉根数","客户问题数量"],"只计complaint_opened根事件，reply/resolved不新增根；根包含配送查询和安装等一般咨询，不都代表严重投诉。",feedback_base,
        time_fields=["customer_feedback.occurred_at"],predicates=["customer_feedback.event_type='complaint_opened'"],unit="contact_roots",tax="not_applicable",risks=["同订单可以多根；不可用所有反馈行数作投诉单数。"])
    add("customer_contact_order_count","咨询/投诉订单数",["有投诉订单数"],"有complaint_opened的不同order_id数量，与根事件数量不同。",feedback_base,
        deps=["customer_contact_root_count"],time_fields=["customer_feedback.occurred_at"],unit="orders",tax="not_applicable")
    add("customer_contact_order_rate","咨询/投诉订单比例",["投诉订单率"],"同一原发货cohort中有根咨询/投诉的不同订单数/该cohort不同已发货订单数，截至同一as_of。",
        "shipments.order_id shipments.shipped_at",["feedback_order","shipments_order"],deps=["customer_contact_order_count"],time_fields=["shipments.shipped_at"],unit="ratio",tax="not_applicable",maturity="未送达也可咨询，按相同发货cohort和观察长度比较。",risks=["反馈先聚合到订单，不放大分母及金额。"])
    add("first_response_duration","首次客服回复时长",["首响时长","回复时效"],"每根咨询取首个complaint_replied时间-根occurred_at；未回复根单列，不填零。",feedback_base,["feedback_parent"],
        time_fields=["customer_feedback.occurred_at"],unit="hours",tax="not_applicable",predicates=["父为complaint_opened，子为complaint_replied"],maturity="只用截点前已知回复；未回复与真实零时长分开。")
    add("contact_resolution_rate","咨询/投诉解决率",["投诉解决率","客户问题解决率"],"有complaint_resolved的根数/同批根总数，按根cohort和as_of统计，不把回复状态等同解决。",feedback_base,["feedback_parent"],
        deps=["customer_contact_root_count"],time_fields=["customer_feedback.occurred_at"],unit="ratio",tax="not_applicable",maturity="尚未解决保持开放，不能提前补未来解决。")
    add("review_count","评价数量",["评价数","评论数量"],"只计review_submitted，先区分订单级order_item_id为空与商品级非空。",feedback_base+" customer_feedback.rating",
        time_fields=["customer_feedback.occurred_at"],predicates=["customer_feedback.event_type='review_submitted'"],unit="reviews",tax="not_applicable",risks=["订单级评价不分配给每个SKU；多回复不是评价。"])
    add("review_mean","评价均分",["平均评分","评分分布"],"只对指定订单级/商品级review_submitted的1—5分评分求均值和分布，未评价不记0。","customer_feedback.rating",
        deps=["review_count"],unit="rating_points_1_to_5",tax="not_applicable",maturity="同一评价覆盖和观察长度；不能把评价样本当全部订单。")
    add("order_review_coverage","订单评价覆盖率",["评价覆盖率"],"同一已送达cohort中有订单级评价的不同订单数/该cohort已送达不同订单数；商品级覆盖另列。",
        "shipments.order_id shipments.shipped_at shipments.delivered_at shipments.delivery_recorded_at",["feedback_order","shipments_order"],deps=["review_count"],predicates=["customer_feedback.order_item_id IS NULL","shipments.delivered_at IS NOT NULL"],unit="ratio",tax="not_applicable",maturity="相同原订单/发货cohort及as_of。")
    add("goods_refund_amount_ratio","原成交商品退款金额比例",["商品退款率","退款金额率"],"同一原capture cohort截至as_of的已完成商品含税退款/原商品GMV，顾客运费另列。",
        "refunds.order_item_id refunds.completed_at refunds.completion_recorded_at refunds.item_refund_gross_cents order_items.order_item_id order_items.order_id",
        ["refunds_item"],deps=["paid_goods_gmv_gross"],unit="ratio",tax="gross",refund="completed_in_original_capture_cohort",maturity="近期cohort售后未成熟；不能用本月退款/本月GMV冒充原订单最终退款率。")
    return metrics


def glossary():
    return [
        {"term":term,"candidate_metrics":ids,"clarification":text,"requires_disclosure":True}
        for term,ids,text in [
            ("销售额/收入",["paid_goods_gmv_gross","shipped_goods_revenue_ex_vat","net_sales_ex_vat"],"需区分支付GMV、发货收入与扣退净销售；注明含税、运费、退款和时间归属，不能互作同义词。"),
            ("利润",["goods_profit_ex_vat","direct_operating_contribution_ex_vat"],"先区分商品毛利与扣直接费用的贡献；缺固定费用不能声称企业净利润。"),
            ("订单量",["paid_order_count"],"已创建订单、成功支付、发货与取消是不同集合；默认成功支付必须声明，不能按明细行数计订单。"),
            ("转化率",["checkout_session_cvr","sku_view_session_cvr","ad_landing_rate"],"确定会话购买、商品浏览到购买还是点击落地；分子分母必须同一cohort，广告归因窗不同于会话支付窗。"),
            ("ROAS",["goods_roas_ex_vat","refund_adjusted_roas"],"默认不含税商品退款前成交/不含税媒体费，按点击cohort；含税或退款后口径须另命名并注明as_of。"),
            ("退款",["completed_cash_refunds_gross","shipped_goods_refunds_ex_vat","resalable_cost_recovery_ex_vat"],"申请、批准、资金完成、退回和验收不同；现金退回不等于商品收入冲减或成本恢复。"),
            ("净/net",["net_sales_ex_vat"],"ex_vat表示不含税，net_sales表示扣指定退款后的净销售，二者不能混为一个含义。"),
            ("库存",["on_hand_inventory","reserved_inventory","available_inventory"],"区分实物在库、订单预占与可售；在途采购不是可售，余额不可跨日相加。"),
            ("缺货损失",["stockout_duration"],"可核验时长、不可售访问及结账阻断；这些不是精确反事实失单或损失毛利。"),
            ("投诉",["customer_contact_root_count","customer_contact_order_rate"],"complaint_opened包含一般客户咨询；根、回复、解决、评价不同类型，不能把全部行计作严重投诉。"),
            ("客户/用户",["sessions_count"],"visitor_id是伪匿名可观察标识，会话是访问单位；不能声称真实自然人全生命周期或竞品成交去向。"),
            ("本月/最近/今天",[],"按当前绑定快照的as_of_local_date解释场景时间，不使用机器今天；缺少范围时声明默认，不能访问另一快照补未来。"),
        ]
    ]


def build_one(snapshot, public_root, output_root, sections, grains, dictionary_path):
    database,prefix=SNAPSHOT_BINDINGS[snapshot]
    source=public_root/snapshot
    metadata_path=source/'dataset_metadata.json'
    metadata=json.loads(metadata_path.read_text())
    if metadata.get('snapshot_id')!=snapshot or set(metadata['business_tables'])!=set(TABLE_NAMES):
        raise ValueError('Unexpected public snapshot metadata: '+snapshot)
    source_hashes={}
    for table in TABLE_NAMES:
        path=source/(table+'.csv')
        with path.open(encoding='utf-8-sig',newline='') as stream:
            actual=csv.DictReader(stream).fieldnames
        expected=[f['name'] for f in sections[table]['fields']]
        if actual!=expected:
            raise ValueError('Dictionary/header mismatch: '+snapshot+'/'+table)
        source_hashes[path.name]=sha256(path)
        if source_hashes[path.name]!=metadata['snapshot_csv_sha256'][path.name]:
            raise ValueError('Public CSV differs from its declared snapshot hash: '+table)
    ids={table:prefix+'_'+table for table in TABLE_NAMES}
    relations=[]
    for key,parent,pfield,child,cfield,cardinality,nullable,note in RELATIONS:
        relations.append({'id':prefix+'_'+key,'left_table':ids[parent],'left_table_name':parent,'left_field':pfield,
                          'right_table':ids[child],'right_table_name':child,'right_field':cfield,'relation_type':'foreign_key',
                          'cardinality':cardinality,'cardinality_basis':'declared_logical_contract','child_key_nullable':nullable,
                          'description':note,'duplicate_risk':('父事实连接多条子事实会复制父金额，应先聚合/去重。' if cardinality=='1:N' else '可空子事实不能误当每个父对象都有记录。')})
    join_ids={key:prefix+'_'+key for key, *_ in RELATIONS}
    fk_set={(r['right_table_name'],r['right_field']) for r in relations}
    tables=[]; docs=[]; known_fields=set()
    for name in TABLE_NAMES:
        parsed=sections[name]; fields=[]
        for entry in parsed['fields']:
            field=entry['name']; known_fields.add(name+'.'+field)
            dbtype,displaytype=sql_type(entry['type_text'])
            role=('time' if dbtype in {'TIMESTAMPTZ','DATE'} else 'identifier' if field in PRIMARY_KEYS[name] else
                  'foreign_key' if (name,field) in fk_set else 'filter' if field in {'status','status_as_of','event_type','refund_kind','report_complete','collection_status','is_internal_traffic','identity_linkable'} else
                  'metric' if field.endswith('_cents') or field in {'quantity','ordered_qty','accepted_qty','on_hand_delta','reserved_delta','restock_qty','returned_qty','refund_qty','clicks','impressions','rating'} else 'dimension')
            unit=('EUR_cents_gross' if field.endswith('_gross_cents') else 'EUR_cents_ex_vat' if field.endswith('_ex_vat_cents') else
                  'EUR_cents_vat' if field.endswith('_vat_cents') else entry['type_text'].split('，',1)[1] if '，' in entry['type_text'] else dbtype)
            alias=[field.replace('_',' '),field_label(entry['description'],field)]
            definition={'name':field,'label':field_label(entry['description'],field),'type':displaytype,'sql_type':dbtype,
                        'description':entry['description'],'aliases':alias,'role':role,'aggregation':'none',
                        'unit':unit,'nullable':('可空' in entry['type_text'] or '可空' in entry['description'] or '为空' in entry['description']),
                        'null_semantics':entry['description'],'source_type_text':entry['type_text']}
            if name+'.'+field in ENUMS:
                definition['enum_values']=ENUMS[name+'.'+field]
                definition['description']+=' 允许值：'+', '.join(ENUMS[name+'.'+field])+'。'
            fields.append(definition)
        dependencies=TABLE_DEPENDENCIES[name].split()
        if not set(dependencies)<=set(f['name'] for f in fields):
            raise ValueError('Invalid dependency closure: '+name)
        tables.append({'id':ids[name],'name':name,'label':parsed['label'],'database':database,'domain':DOMAINS[name],
                       'description':parsed['description'],'business_terms':[parsed['label'],DOMAINS[name]],
                       'grain':grains[name],'primary_key':PRIMARY_KEYS[name],'unique_keys':UNIQUE_KEYS.get(name,[]),
                       'dependency_fields':dependencies,'fields':fields,
                       'duplicate_risks':['先按事实粒度和各自时间聚合，再与其他事实表连接；状态和金额必须遵循指标定义。']})
    metrics=metrics_catalog(); by_metric={m['id']:m for m in metrics}
    def close_metric(ident,stack=()):
        if ident in stack:
            raise ValueError('Metric dependency cycle: '+ident)
        metric=by_metric[ident]
        refs=set(metric['direct_fields']); joins=set(metric['join_keys'])
        for dependency in metric['depends_on']:
            dep_refs,dep_joins=close_metric(dependency,stack+(ident,))
            refs.update(dep_refs); joins.update(dep_joins)
        return refs,joins
    expanded={metric['id']:close_metric(metric['id']) for metric in metrics}
    relation_specs={row[0]:row for row in RELATIONS}
    def component_ids(ident):
        selected={ident}
        for dependency in by_metric[ident]['depends_on']:
            selected.update(component_ids(dependency))
        return selected
    for metric in metrics:
        refs,joins=expanded[metric['id']]
        if not refs<=known_fields or not joins<=set(join_ids):
            raise ValueError('Metric has unknown field or join: '+metric['id'])
        for key in joins:
            _,parent,pfield,child,cfield,*_=relation_specs[key]
            refs.update((parent+'.'+pfield,child+'.'+cfield))
        refs.update(table+'.'+field for table in {x.split('.')[0] for x in refs} for field in TABLE_DEPENDENCIES[table].split())
        metric['required_fields']=[{'table':ref.split('.')[0],'table_id':ids[ref.split('.')[0]],'field':ref.split('.')[1],
                                    'field_id':ids[ref.split('.')[0]]+'.'+ref.split('.')[1]} for ref in sorted(refs)]
        metric['fact_tables']=sorted({x.split('.')[0] for x in refs})
        metric['join_dependencies']=[join_ids[key] for key in sorted(joins)]
        metric['component_policies']=[{'metric_id':ident,'definition':by_metric[ident]['definition'],
                                       'required_predicates':by_metric[ident]['required_predicates'],
                                       'time_fields':by_metric[ident]['time_fields'],
                                       'maturity':by_metric[ident]['maturity']}
                                      for ident in sorted(component_ids(metric['id']))]
        metric.pop('direct_fields'); metric.pop('join_keys')
    metric_mentions={ref['field_id']:[] for table in tables for f in table['fields'] for ref in [{'field_id':table['id']+'.'+f['name']}]}
    for metric in metrics:
        for field in metric['required_fields']:
            metric_mentions[field['field_id']].append(metric['id'])
    for table in tables:
        for field in table['fields']:
            ident=table['id']+'.'+field['name']; metric_ids=metric_mentions[ident]
            aliases=list(dict.fromkeys(field['aliases']+[by_metric[m]['label'] for m in metric_ids if field['role']=='metric']))
            related=[r['id'] for r in relations if (r['left_table']==table['id'] and r['left_field']==field['name']) or
                     (r['right_table']==table['id'] and r['right_field']==field['name'])]
            text=(f"字段{table['name']}.{field['name']}，{field['label']}；表粒度：{table['grain']}；"
                  f"定义：{field['description']}；类型/单位：{field['sql_type']}/{field['unit']}。")
            docs.append({'doc_id':ident,'database_id':database,'table_id':table['id'],'table_label':table['label'],
                         'field_name':field['name'],'field_label':field['label'],'field_type':field['type'],
                         'field_description':field['description'],'field_role':field['role'],'aliases':aliases,
                         'samples':[],'profile':'中性逻辑类型与单位资料，未统计数值分布。','keyword_text':' '.join([table['name'],field['name'],*aliases]),
                         'semantic_text':text,'rerank_text':text,'relation_ids':related})
    schema={'format_version':'homenova-public-semantics-v1','database':database,'snapshot_id':snapshot,
            'table_count':len(tables),'field_count':len(docs),'tables':tables,'relations':relations,
            'signature':signature({'snapshot':snapshot,'tables':tables,'relations':relations})}
    capabilities={'database':database,'snapshot_id':snapshot,'scenario_clock_local_date':metadata['as_of_local_date'],
                  'observation_cutoff_at':metadata['observation_cutoff_at'],'cutoff_comparison':'strictly_before',
                  'timezone':metadata['timezone'],'currency':'EUR','currency_unit':'integer EUR cents',
                  'visit_start_at':metadata['visit_start_at'],'visit_end_at':metadata['visit_end_at'],
                  'ad_complete_through_local_date':metadata.get('ad_complete_through_local_date'),
                  'ad_reporting_policy':metadata.get('ad_reporting_policy'),
                  'supported_metric_ids':[m['id'] for m in metrics],
                  'boundaries':['只读当前绑定截面，不能使用另一截面的未来字段或终态。','月底广告日结可尚未可知；点击费事实与已上报日记录分别说明。',
                                '广告按7×24小时末次付费点击归因，归因销售不等于广告增量销售。','经营停止前七日点击缺乏完整后续购买机会；尾窗过去不自动消除右截断。',
                                '退款完成与退货验收不同；近期cohort售后未成熟，未完成保持未知。','客户咨询/投诉根包含一般咨询；主诉、相关性与评分不能证明唯一原因。',
                                '没有竞品成交、未成交反事实、自然人全生命周期或全部企业固定费用，不能给精确流失、竞品抢单或企业净利润。'],
                  'safe_failure':'缺少必要事实、口径或范围时说明缺口/澄清；可返回带证据的部分结论，不编造未知金额。'}
    metric_payload={'format_version':'homenova-metric-contract-v1','database':database,'snapshot_id':snapshot,
                    'metrics':metrics,'metric_dependencies':{m['id']:m['depends_on'] for m in metrics},
                    'join_dependencies':{r['id']:r for r in relations},
                    'reconciliation':['成功扣款=商品折后含税额+顾客含税运费。','商品含税=商品不含税+逐行税额。',
                                      '净销售=发货商品不含税收入-完成的已发货商品不含税退款。','商品毛利=净销售-发货COGS+实际验收可再售成本恢复。',
                                      '可售=期初+验收采购+可再售退回-发货-当前预占；支付不重复扣库。',
                                      '完整广告日消耗=相同campaign与当地点击日的点击费用和。'],
                    'fee_allocation_policy':'默认店铺发生期；订单共享费用按原商品行折后不含税金额最大余数分配，广告费归计划目标SKU，属于管理分摊而非因果贡献。'}
    target=output_root/snapshot; target.mkdir(parents=True,exist_ok=True)
    assets={'schema.json':schema,'field_documents.json':{'database':database,'snapshot_id':snapshot,
             'embedding_status':'not_built','is_loadable_vector_store':False,'documents':docs},
            'metrics.json':metric_payload,'glossary.json':{'database':database,'snapshot_id':snapshot,'terms':glossary()},
            'capabilities.json':capabilities,'dataset_metadata.json':metadata,
            'datasource.json':{'id':database,'database':database,'snapshot_id':snapshot,
                               'relative_data_path':'data/homenova_phase_d/public/'+snapshot,
                               'scenario_clock_local_date':metadata['as_of_local_date'],'observation_cutoff_at':metadata['observation_cutoff_at']}}
    for filename,payload in assets.items():
        (target/filename).write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    manifest={'format_version':'homenova-semantic-manifest-v1','database':database,'snapshot_id':snapshot,
              'sources':{'dictionary_sha256':sha256(dictionary_path),'metadata_sha256':sha256(metadata_path),
                         'csv_sha256':source_hashes,'interpreted_inputs':'public dictionary, current metadata and CSV headers only'},
              'table_count':len(tables),'field_count':len(docs),'relation_count':len(relations),'metric_count':len(metrics),
              'field_documents_are_embeddings':False,'asset_sha256':{name:sha256(target/name) for name in assets},
              'self_checks':{'all_207_fields_documented':len(docs)==207,'all_metric_fields_exist':True,
                             'all_dependency_fields_exist':True,'all_joins_reference_real_columns':True,
                             'no_observed_value_profiles_or_results':True,'snapshot_binding_isolated':True}}
    for relation in relations:
        for side in ('left','right'):
            name=relation[side+'_table_name']; field=relation[side+'_field']
            if name+'.'+field not in known_fields:
                raise ValueError('Unknown relation endpoint: '+relation['id'])
    (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return {'snapshot_id':snapshot,'database':database,'tables':len(tables),'fields':len(docs),
            'relations':len(relations),'metrics':len(metrics),'output':str(target.resolve())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-root',type=Path,default=ROOT/'data/homenova_phase_d/public')
    parser.add_argument('--output',type=Path,default=ROOT/'data/homenova_phase_e/public_semantics')
    args=parser.parse_args()
    dictionary_path=args.public_root/'asof_2026_08_31/data_dictionary.md'
    text=dictionary_path.read_text()
    sections,grains=parse_dictionary(text)
    for values in ENUMS.values():
        if any(value not in text for value in values):
            raise ValueError('Enum not supported by the allowed public dictionary')
    summaries=[build_one(snapshot,args.public_root,args.output,sections,grains,dictionary_path) for snapshot in SNAPSHOT_BINDINGS]
    print(json.dumps({'outputs':summaries},ensure_ascii=False))


if __name__=='__main__':
    main()

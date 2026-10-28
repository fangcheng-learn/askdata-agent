"""One local SKU profitability query, checked against independent public CSV.

Uses July 2026 occurrence-period accounting in the August public snapshot.
Paid units, shipment revenue/COGS, completed refunds and inspected recovery use
their own event times; this is merchandise gross profit, not enterprise profit.
No model, Agent endpoint, HTTP, retrieval, private answer, or generator is used.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'data/homenova_harness_v1/private/sql_probe_v5/profit_probe.json'
PUBLIC = ROOT / 'data/homenova_phase_d/public/asof_2026_08_31'
DATABASE = 'homenova_20260831'
START = '2026-07-01T00:00:00+02:00'
END = '2026-08-01T00:00:00+02:00'
sys.path.insert(0, str(ROOT / 'references/askdata_studio/backend'))


def deny_network(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'subprocess.Popen'}:
        raise RuntimeError('Profit probe forbids network and external processes')


sys.addaudithook(deny_network)

from app.homenova import schema_payload
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.security import AccessController


SQL = f"""WITH paid_orders AS (
  SELECT DISTINCT p.order_id
  FROM payments p
  WHERE p.status='captured' AND p.captured_at>='{START}' AND p.captured_at<'{END}'
), paid_units AS (
  SELECT oi.sku_id, SUM(oi.quantity) AS paid_qty
  FROM paid_orders p JOIN order_items oi ON p.order_id=oi.order_id
  GROUP BY oi.sku_id
), shipped_facts AS (
  SELECT oi.sku_id, SUM(oi.line_ex_vat_cents) AS shipped_revenue_cents,
         SUM(oi.line_cost_ex_vat_cents) AS shipped_cogs_cents
  FROM shipments s JOIN order_items oi ON s.order_id=oi.order_id
  WHERE s.shipped_at>='{START}' AND s.shipped_at<'{END}'
  GROUP BY oi.sku_id
), completed_refunds AS (
  SELECT oi.sku_id, SUM(r.item_refund_ex_vat_cents) AS completed_refund_cents
  FROM refunds r JOIN order_items oi ON r.order_item_id=oi.order_item_id
  JOIN shipments s ON oi.order_id=s.order_id
  WHERE r.completed_at>='{START}' AND r.completed_at<'{END}'
    AND r.refund_kind<>'pre_ship_cancel' AND s.shipped_at IS NOT NULL
  GROUP BY oi.sku_id
), inspected_recovery AS (
  SELECT oi.sku_id, SUM(r.recovered_cost_ex_vat_cents) AS recovered_cost_cents
  FROM refunds r JOIN order_items oi ON r.order_item_id=oi.order_item_id
  WHERE r.inspected_at>='{START}' AND r.inspected_at<'{END}'
    AND r.refund_kind='return_and_refund'
  GROUP BY oi.sku_id
), combined AS (
  SELECT sk.sku_id, sk.sku_name, COALESCE(pu.paid_qty,0) AS paid_qty,
         COALESCE(sf.shipped_revenue_cents,0) AS shipped_revenue_cents,
         COALESCE(sf.shipped_cogs_cents,0) AS shipped_cogs_cents,
         COALESCE(cr.completed_refund_cents,0) AS completed_refund_cents,
         COALESCE(ir.recovered_cost_cents,0) AS recovered_cost_cents
  FROM sku_catalog sk
  LEFT JOIN paid_units pu ON sk.sku_id=pu.sku_id
  LEFT JOIN shipped_facts sf ON sk.sku_id=sf.sku_id
  LEFT JOIN completed_refunds cr ON sk.sku_id=cr.sku_id
  LEFT JOIN inspected_recovery ir ON sk.sku_id=ir.sku_id
), calculated AS (
  SELECT sku_id, sku_name, paid_qty, shipped_revenue_cents, shipped_cogs_cents,
         completed_refund_cents, recovered_cost_cents,
         shipped_revenue_cents-completed_refund_cents AS net_sales_cents
  FROM combined
)
SELECT sku_id, sku_name, paid_qty, shipped_revenue_cents, shipped_cogs_cents,
       completed_refund_cents, recovered_cost_cents, net_sales_cents,
       net_sales_cents-shipped_cogs_cents+recovered_cost_cents AS goods_profit_cents,
       CASE WHEN net_sales_cents>0
         THEN (net_sales_cents-shipped_cogs_cents+recovered_cost_cents)*1.0/net_sales_cents
         ELSE NULL END AS goods_margin
FROM calculated
ORDER BY paid_qty DESC, sku_id"""


def contract():
    fields = ['order_items.line_ex_vat_cents', 'order_items.line_cost_ex_vat_cents',
              'refunds.item_refund_ex_vat_cents', 'refunds.recovered_cost_ex_vat_cents']
    return dict(original_question='核对2026年7月各SKU支付件数与发生期商品净销售、商品毛利润和毛利率',
                data_source=DATABASE, tables=['payments','order_items','shipments','refunds','sku_catalog'],
                metrics=[dict(name='quantity', source_fields=['order_items.quantity'], aggregation='sum'),
                         dict(name='shipped_goods_revenue_ex_vat', source_fields=['order_items.line_ex_vat_cents'], aggregation='sum'),
                         dict(name='shipped_goods_cogs_ex_vat', source_fields=['order_items.line_cost_ex_vat_cents'], aggregation='sum'),
                         dict(name='shipped_goods_refunds_ex_vat', source_fields=['refunds.item_refund_ex_vat_cents'], aggregation='sum'),
                         dict(name='resalable_cost_recovery_ex_vat', source_fields=['refunds.recovered_cost_ex_vat_cents'], aggregation='sum'),
                         dict(name='net_sales_ex_vat', source_fields=[fields[0],fields[2]], aggregation='derived'),
                         dict(name='goods_profit_ex_vat', source_fields=fields, aggregation='derived'),
                         dict(name='goods_margin', source_fields=fields, aggregation='derived')],
                dimensions=['sku_catalog.sku_id','sku_catalog.sku_name'],
                filters=[dict(field='payments.status',operator='=',value='captured'),
                         dict(field='refunds.refund_kind',operator='<>',value='pre_ship_cancel'),
                         dict(field='refunds.refund_kind',operator='=',value='return_and_refund')],
                time_range=None,
                time_ranges=[dict(field=field,start=START,end=END) for field in
                             ['payments.captured_at','shipments.shipped_at','refunds.completed_at','refunds.inspected_at']],
                grain='dimension',assumptions=['按各分量自身发生日期进入2026年7月，非原订单支付月份cohort',
                                               '销量为成功支付销售单位，套装按套计',
                                               '商品毛利润不含税、顾客运费、广告支付物流费用及企业固定费用'],
                open_questions=[])


def read(name):
    with (PUBLIC/(name+'.csv')).open(encoding='utf-8',newline='') as handle:
        return list(csv.DictReader(handle))


def in_period(value):
    if not value:return False
    parsed=datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Berlin'))
    return parsed.strftime('%Y-%m')=='2026-07'


def independent():
    catalog=read('sku_catalog');items=read('order_items');payments=read('payments');shipments=read('shipments');refunds=read('refunds')
    item_by_id={item['order_item_id']:item for item in items}
    items_by_order=defaultdict(list)
    for item in items:items_by_order[item['order_id']].append(item)
    buckets={sku['sku_id']:dict(sku_id=sku['sku_id'],sku_name=sku['sku_name'],paid_qty=0,shipped_revenue_cents=0,
                                shipped_cogs_cents=0,completed_refund_cents=0,recovered_cost_cents=0) for sku in catalog}
    captured=set()
    for payment in payments:
        if payment['status']=='captured' and in_period(payment['captured_at']):
            assert payment['order_id'] not in captured,'Successful captures must be unique before joining items'
            captured.add(payment['order_id'])
            for item in items_by_order[payment['order_id']]:buckets[item['sku_id']]['paid_qty']+=int(item['quantity'])
    shipped_orders=set()
    for shipment in shipments:
        if shipment['shipped_at']:
            assert shipment['order_id'] not in shipped_orders,'Full-order shipments must be unique'
            shipped_orders.add(shipment['order_id'])
        if in_period(shipment['shipped_at']):
            for item in items_by_order[shipment['order_id']]:
                buckets[item['sku_id']]['shipped_revenue_cents']+=int(item['line_ex_vat_cents'])
                buckets[item['sku_id']]['shipped_cogs_cents']+=int(item['line_cost_ex_vat_cents'])
    recovery_component_checks=[]
    for refund in refunds:
        item=item_by_id[refund['order_item_id']];bucket=buckets[item['sku_id']]
        if in_period(refund['completed_at']) and refund['refund_kind']!='pre_ship_cancel' and item['order_id'] in shipped_orders:
            bucket['completed_refund_cents']+=int(refund['item_refund_ex_vat_cents'])
        if in_period(refund['inspected_at']) and refund['refund_kind']=='return_and_refund':
            computed=int(refund['restock_qty'])*int(item['unit_cost_ex_vat_cents'])
            assert computed==int(refund['recovered_cost_ex_vat_cents'])
            bucket['recovered_cost_cents']+=computed
            recovery_component_checks.append(dict(refund_line_id=refund['refund_line_id'],computed_cost_cents=computed,
                                                  refund_completed_in_july=in_period(refund['completed_at'])))
    for bucket in buckets.values():
        bucket['net_sales_cents']=bucket['shipped_revenue_cents']-bucket['completed_refund_cents']
        bucket['goods_profit_cents']=bucket['net_sales_cents']-bucket['shipped_cogs_cents']+bucket['recovered_cost_cents']
        bucket['goods_margin']=Decimal(bucket['goods_profit_cents'])/Decimal(bucket['net_sales_cents']) if bucket['net_sales_cents']>0 else None
    return list(buckets.values()),recovery_component_checks


def main():
    if OUTPUT.exists():raise RuntimeError('Preserve probe history; profit_probe.json already exists')
    files=[PUBLIC/(name+'.csv') for name in ['sku_catalog','order_items','payments','shipments','refunds']]
    files += [ROOT/'data/homenova_harness_v1/private/implementation_v2/paid_runs.json',
              ROOT/'data/homenova_harness_v1/private/validation_v3_real/paid_runs.json']
    fingerprint=lambda:{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    before=fingerprint();spec=contract();engine=DuckDbEngine();scope=AccessController().scoped('demo_admin',DATABASE)
    graph=schema_payload(DATABASE);started=time.monotonic()
    record=dict(mode='one hand-written local profitability SQL; not live Agent acceptance',datasource_id=DATABASE,
                period=dict(start=START,end=END,timezone='Europe/Berlin'),sql=SQL,contract=spec,
                new_paid_calls=0,live_model_calls=0,http_calls=0,embedding_calls=0,rerank_calls=0,
                network_guard_installed=True,amount_unit='integer EUR cents ex VAT',
                note='Paid units and profit components use their own occurrence periods, not the same order cohort. Merchandise gross profit is not enterprise net profit.')
    try:
        validated=QueryContractValidator(engine).validate(spec,SQL,spec['original_question'],DATABASE,graph,scope.public())
    except (ValueError,KeyError,TypeError) as exc:
        record.update(validation_passed=False,error=str(exc),executed=False,engine_preflight_error=engine.preflight(DATABASE,SQL,scope))
    else:
        record.update(validation_passed=True,validated_contract=validated,pending_checks=validated['validation']['pending_checks'])
        execution=engine.execute(DATABASE,SQL,scope);record['execution']=asdict(execution);record['executed']=True
        if execution.success:
            expected,recovery=independent();by_id={row['sku_id']:row for row in expected};checks=[]
            for actual in execution.rows:
                wanted=by_id[actual['sku_id']];fields={}
                for name in wanted:
                    if name=='goods_margin':
                        fields[name]=(actual[name] is None and wanted[name] is None) or (actual[name] is not None and wanted[name] is not None and abs(Decimal(str(actual[name]))-wanted[name])<Decimal('0.00000001'))
                    else:fields[name]=actual[name]==wanted[name]
                checks.append(dict(sku_id=actual['sku_id'],fields=fields,matched=all(fields.values())))
            record['independent_public_csv_check']=dict(rows=expected,checks=checks,all_skus_present=set(by_id)=={r['sku_id'] for r in execution.rows},
                                                       recovery_recomputed_from_restock_and_order_unit_cost=recovery,
                                                       recovered_without_july_refund_completion=sum(not r['refund_completed_in_july'] for r in recovery))
    after=fingerprint();assert before==after
    record.update(elapsed_seconds=round(time.monotonic()-started,4),public_and_ledger_inputs_unchanged=True,input_sha256=before)
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    with OUTPUT.open('x',encoding='utf-8') as handle:json.dump(record,handle,ensure_ascii=False,indent=2,default=str);handle.write('\n')
    verification=record.get('independent_public_csv_check',{})
    print(json.dumps(dict(output=str(OUTPUT),validation_passed=record['validation_passed'],executed=record['executed'],
                          error=record.get('error'),rows=len(record.get('execution',{}).get('rows',[])),
                          independent_all_match=all(c['matched'] for c in verification.get('checks',[])) if verification else None,
                          recovered_without_july_refund_completion=verification.get('recovered_without_july_refund_completion'),
                          new_paid_calls=0),ensure_ascii=False))


if __name__=='__main__':main()

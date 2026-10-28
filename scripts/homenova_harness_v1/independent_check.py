"""Offline independent arithmetic from the selected public CSV, no Agent imports.

Run after Agent output. This file and its output must never enter runtime prompts.
"""
import csv
import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[2]
folder=ROOT/'data/homenova_phase_d/public/asof_2026_08_31'

def read(name):return list(csv.DictReader((folder/(name+'.csv')).open()))
def month(value):return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Berlin')).strftime('%Y-%m')
orders={row['order_id']:row for row in read('orders')}
sessions={row['session_id']:row for row in read('sessions')}
catalog={row['sku_id']:row for row in read('sku_catalog')}
campaigns={row['campaign_id']:row for row in read('campaigns')}
items=defaultdict(list)
for row in read('order_items'):items[row['order_id']].append(row)
aggregates=defaultdict(lambda:{'gmv_cents':0,'units':0,'order_ids':set()})
seen=set()
for payment in read('payments'):
 if payment['status']!='captured':continue
 assert payment['order_id'] not in seen,'Multiple successful captures require explicit dedup policy'
 seen.add(payment['order_id']);period=month(payment['captured_at']);order=orders[payment['order_id']]
 channel=campaigns.get(order.get('attributed_campaign_id'),{}).get('channel_code') or 'Organic/Direct'
 source_channel=sessions.get(order.get('checkout_session_id'),{}).get('source_channel') or 'Unknown'
 for item in items[payment['order_id']]:
  category=catalog[item['sku_id']]['category_code'];amount=int(item['line_paid_gross_cents']);qty=int(item['quantity'])
  for dimension,key in [('overall','all'),('category',category),('sku',item['sku_id']),('channel',channel),('category_channel',category+'|'+channel),('source_channel',source_channel),('category_source',category+'|'+source_channel)]:
   target=aggregates[(period,dimension,key)];target['gmv_cents']+=amount;target['units']+=qty;target['order_ids'].add(order['order_id'])
rows=[{'month':period,'dimension':dimension,'key':key,'gmv_cents':v['gmv_cents'],'paid_orders':len(v['order_ids']),
       'paid_units':v['units'],'aov_cents':v['gmv_cents']/len(v['order_ids'])} for (period,dimension,key),v in sorted(aggregates.items())]
parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'data/homenova_harness_v1/private/independent_facts_final.json');args=parser.parse_args()
output=args.output
if output.exists():parser.error('use a new output filename')
output.write_text(json.dumps({'source':'public CSV, independent standard-library arithmetic','rows':rows},indent=2,ensure_ascii=False))
print(json.dumps([row for row in rows if row['dimension']=='overall'],ensure_ascii=False))

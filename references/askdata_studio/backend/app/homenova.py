"""Allowlisted HomeNova snapshots and neutral, dependency-aware business semantics.

This module reads only the selected D snapshot and its E public schema/metrics.
It never opens generators, answers, private audits or the storyline manual.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "homenova_phase_d" / "public"
SEMANTIC_ROOT = PROJECT_ROOT / "data" / "homenova_phase_e" / "public_semantics"
RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "data" / "homenova_runtime"
SOURCES = {
    "homenova_20260831": ("asof_2026_08_31", "HomeNova · 截至2026-08-31"),
    "homenova_20261015": ("asof_2026_10_15", "HomeNova · 截至2026-10-15"),
}


def is_homenova(database: str | None) -> bool:
    return str(database or "") in SOURCES


def snapshot_folder(database: str) -> Path:
    if database not in SOURCES:
        raise ValueError("未知HomeNova数据源")
    folder = (SNAPSHOT_ROOT / SOURCES[database][0]).resolve()
    if folder.parent != SNAPSHOT_ROOT.resolve() or not folder.is_dir():
        raise ValueError("HomeNova公开快照不可用")
    return folder


def semantic_folder(database: str) -> Path:
    if database not in SOURCES:
        raise ValueError("未知HomeNova数据源")
    folder = (SEMANTIC_ROOT / SOURCES[database][0]).resolve()
    if folder.parent != SEMANTIC_ROOT.resolve() or not folder.is_dir():
        raise ValueError("HomeNova公开语义尚未发布")
    return folder


@lru_cache(maxsize=2)
def schema_payload(database: str) -> dict[str, Any]:
    return json.loads((semantic_folder(database) / "schema.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=2)
def metrics(database: str) -> list[dict[str, Any]]:
    payload = json.loads((semantic_folder(database) / "metrics.json").read_text(encoding="utf-8"))
    return list(payload.get("metrics") or []) if isinstance(payload, dict) else list(payload)


def available_sources() -> list[dict[str, Any]]:
    output = []
    for database, (_, label) in SOURCES.items():
        try:
            schema_payload(database)
            output.append({"id": database, "label": label, "scenario": scenario(database)})
        except (OSError, ValueError):
            continue
    return output


def registered_schema() -> tuple[list[dict], list[dict]]:
    tables, relations = [], []
    for source in available_sources():
        payload = schema_payload(source["id"])
        tables.extend(payload.get("tables") or [])
        relations.extend(payload.get("relations") or [])
    return tables, relations


@lru_cache(maxsize=2)
def scenario(database: str) -> dict[str, Any]:
    meta = json.loads((snapshot_folder(database) / "dataset_metadata.json").read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat(meta["observation_cutoff_at"].replace("Z", "+00:00"))
    clock = (cutoff - timedelta(seconds=1)).astimezone(ZoneInfo("Europe/Berlin"))
    return {"analysis_date": clock.date().isoformat(), "analysis_datetime": clock.isoformat(timespec="seconds"),
            "timezone": "Europe/Berlin", "currency": "EUR", "currency_unit": "integer EUR cents",
            "observation_cutoff_at": meta["observation_cutoff_at"], "visit_start_at": meta["visit_start_at"],
            "visit_end_at": meta["visit_end_at"], "ad_complete_through_local_date": meta.get("ad_complete_through_local_date")}


@lru_cache(maxsize=2)
def public_capabilities(database: str) -> dict[str, Any]:
    return json.loads((semantic_folder(database) / "capabilities.json").read_text(encoding="utf-8"))


def scenario_text(database: str) -> str:
    capabilities = json.loads((semantic_folder(database) / "capabilities.json").read_text(encoding="utf-8"))
    return ("当前唯一数据源与场景时间：" + json.dumps({"datasource_id": database, **scenario(database)}, ensure_ascii=False)
            + "。相对日期使用analysis_date，不能使用机器今天。所有业务日按Europe/Berlin；UTC时间列为TIMESTAMPTZ。"
            "金额整数欧分，报告EUR时除以100；观察截止为排他边界，未来结果不在此快照。"
            "不要把含税成交额当不含税发货收入、净销售或毛利；按公开指标定义查询，分母为零返回NULL。"
            "当前公开数据能力边界：" + json.dumps(capabilities.get("boundaries", []), ensure_ascii=False)
            + "。缺少必要事实时说明限制，不猜测精确反事实。")


def public_term_notes(database: str, query: str) -> list[dict[str, Any]]:
    """Select terminology notes from this source's public glossary only."""
    payload = json.loads((semantic_folder(database) / "glossary.json").read_text(encoding="utf-8"))
    return [entry for entry in payload.get("terms", [])
            if any(term and term.casefold() in query.casefold()
                   for term in re.split(r"[/／]", str(entry.get("term") or "")))]


def public_datasource(database: str) -> dict[str, Any]:
    return {"id": database, "label": SOURCES[database][1], "scenario": scenario(database)}


def _terms(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z_][a-z_0-9]*|[\u4e00-\u9fff]{2,}", text.casefold()))
    for phrase in list(tokens):
        if re.fullmatch(r"[\u4e00-\u9fff]+", phrase):
            tokens.update(phrase[i:i+2] for i in range(len(phrase)-1))
    return tokens


def selected_retrieval(database: str, query: str, base_retrieval: dict[str, Any],
                       extraction: dict | None = None,
                       confirmed_fields: list[dict] | None = None) -> dict[str, Any]:
    """Close neutral metric dependencies after successful real hybrid retrieval.

    The incoming hits and their BM25/dense/RRF/rerank evidence are retained.
    Dependency additions are labelled separately and never claim model scores.
    """
    payload = schema_payload(database)
    tables = {str(table.get("name") or table["id"]): table for table in payload["tables"]}
    by_id = {table["id"]: table for table in tables.values()}
    query_text = query.casefold()
    query_terms = _terms(query + " " + " ".join((extraction or {}).get("retrieval_terms") or []))
    ranked = []
    for metric in metrics(database):
        names = [str(metric.get("id") or ""), str(metric.get("label") or ""), *(metric.get("aliases") or [])]
        direct = max([len(name) for name in names if name and name.casefold() in query_text] or [0])
        score = direct * 5 + len(query_terms & _terms(" ".join(names)))
        if score:
            ranked.append((score, str(metric.get("id") or ""), metric))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = [item[2] for item in ranked[:6] if item[0] >= max(2, ranked[0][0]*0.16)] if ranked else []
    # Load only the transitive metric definitions needed by this request.
    by_metric = {metric['id']: metric for metric in metrics(database)}
    selected_ids = {metric['id'] for metric in selected}
    for metric in selected:
        for dependency in metric.get('depends_on') or []:
            if dependency in by_metric and dependency not in selected_ids:
                selected.append(by_metric[dependency])
                selected_ids.add(dependency)
    needed: set[tuple[str, str]] = set()
    def add(reference: Any) -> None:
        if isinstance(reference, dict):
            table, field = reference.get("table"), reference.get("field")
        elif isinstance(reference, str) and "." in reference:
            table, field = reference.rsplit(".", 1)
        else:
            return
        table = str(table or "")
        if table in by_id:
            table = str(by_id[table].get("name") or table)
        if table in tables and any(item["name"] == field for item in tables[table]["fields"]):
            needed.add((table, str(field)))
    for metric in selected:
        for reference in metric.get("required_fields") or []:
            add(reference)
    for hit in base_retrieval.get("hits") or []:
        add({"table": hit.get("table_id"), "field": hit.get("field_name")})
    for reference in confirmed_fields or []:
        table = by_id.get(str(reference.get("tableId") or ""))
        if table:
            add({"table": table.get("name"), "field": reference.get("name")})
    if not needed:
        for name in ("orders", "order_items", "payments"):
            for field in tables[name].get("dependency_fields") or tables[name].get("primary_key") or []:
                add({"table": name, "field": field})
    selected_tables = {name for name, field in needed}
    for name in list(selected_tables):
        table = tables[name]
        dependencies = table.get("dependency_fields") or []
        for field in [*(table.get("primary_key") or []), *dependencies]:
            add({"table": name, "field": field})
    original_hits = {hit["doc_id"]: hit for hit in base_retrieval.get("hits") or []}
    hits = []
    for name, field_name in sorted(needed):
        table = tables[name]
        field = next(item for item in table["fields"] if item["name"] == field_name)
        doc_id = f"{table['id']}.{field_name}"
        if doc_id in original_hits:
            hits.append(original_hits[doc_id])
            continue
        hits.append({"doc_id": doc_id, "database_id": database, "table_id": table["id"],
            "table_label": table["label"], "field_name": field_name, "field_label": field.get("label", field_name),
            "field_type": field.get("type", "文本"), "field_description": field.get("description", ""),
            "field_role": field.get("role", ""), "score": None, "source": "public_semantic_dependency_closure",
            "selection_reason": "metric_dependency_or_table_state_time_key", "rerank_score": None,
            "bm25_rank": None, "dense_rank": None, "rrf_score": None})
    version = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {**base_retrieval, "hits": hits, "selected_count": len(hits),
            "hybrid_selected_count": len(original_hits), "dependency_added_count": len(hits) - len(original_hits),
            "selected_semantics": selected, "selected_term_notes": public_term_notes(database, query),
            "semantic_index_signature": version, "datasource_id": database}


def registered_metric(database: str, name: str) -> dict[str, Any] | None:
    if not is_homenova(database):
        return None
    normalized = re.sub(r"\s+", "", name).casefold()
    for metric in metrics(database):
        names = [metric.get("id", ""), metric.get("label", ""), *(metric.get("aliases") or [])]
        if normalized in {re.sub(r"\s+", "", str(item)).casefold() for item in names}:
            return metric
    return None

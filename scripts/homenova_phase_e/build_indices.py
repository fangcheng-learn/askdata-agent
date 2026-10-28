#!/usr/bin/env python3
"""Build and inspect the real AskData hybrid indexes from E public inputs."""
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "references/askdata_studio/backend"))
from app.config import load_env, settings
from app.model_client import ModelClient
from app.retrieval.homenova_index import HomeNovaSchemaIndex


def main():
    api_key = settings.api_key
    if not api_key:
        shared = load_env(Path("/Users/fangchengdemac/Documents/软件开发知识/.env"),
                          allowed_keys={"DASHSCOPE_API_KEY"})
        api_key = shared.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise RuntimeError("Required DASHSCOPE_API_KEY is missing")
    config = replace(settings, api_key=api_key)
    records = []
    for database in ("homenova_20260831", "homenova_20261015"):
        index = HomeNovaSchemaIndex(database, ModelClient(config), config)
        record = {"datasource_id": database, "index_path": str(index.index_path)}
        try:
            index.ensure_built()
            documents = index.documents
            found = index.retrieve("当前商品可售库存", retrieval_terms=["商品", "可售库存"])
            record.update(
                passed=len(documents) == 207 and {doc.database_id for doc in documents} == {database}
                and all(len(doc.dense_vector) == config.embedding_dimensions for doc in documents),
                documents=len(documents), embedding_source=index.embedding_source,
                source_signature=index._schema_signature(), embedding_dimensions=config.embedding_dimensions,
                bm25_count=found.get("bm25_count"), dense_count=found.get("dense_count"),
                rrf_count=found.get("rrf_count"), selected_count=found.get("selected_count"),
                rerank_source=found.get("rerank_source"),
                schema_only_probe="当前商品可售库存; no business answers or private references",
            )
        except Exception as error:
            message = str(error).replace(api_key, "[REDACTED]")
            record.update(passed=False, error=type(error).__name__ + ": " + message)
        records.append(record)
        print(json.dumps({key: value for key, value in record.items() if key != "error"}, ensure_ascii=False))
    output = ROOT / "data/homenova_phase_e/private/hybrid_index_checks.json"
    output.write_text(json.dumps({"passed": all(row["passed"] for row in records), "indexes": records},
                                 ensure_ascii=False, indent=2) + "\n")
    if not all(row["passed"] for row in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

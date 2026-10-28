"""HomeNova sources for AskData's existing hybrid SchemaIndex pipeline."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import homenova
from ..config import Settings
from ..model_client import ModelClient
from .service import SchemaIndex


class HomeNovaSchemaIndex(SchemaIndex):
    """Only source documents, signature and persistence location differ.

    Embedding, BM25, dense retrieval, RRF and reranking are inherited unchanged.
    An upstream model failure therefore raises the same explicit pipeline error.
    """

    def __init__(self, database: str, model_client: ModelClient, config: Settings) -> None:
        if not homenova.is_homenova(database):
            raise ValueError("未知HomeNova索引数据源")
        self.database = database
        super().__init__(model_client, config,
                         homenova.RUNTIME_ROOT / database / "schema_store.json")

    def _build_raw_documents(self) -> list[dict[str, Any]]:
        payload = json.loads((homenova.semantic_folder(self.database) / "field_documents.json").read_text(encoding="utf-8"))
        documents = list(payload["documents"])
        allowed = {f"{table['id']}.{field['name']}" for table in homenova.schema_payload(self.database)["tables"]
                   for field in table["fields"]}
        if len(documents) != len(allowed) or {doc["doc_id"] for doc in documents} != allowed:
            raise ValueError("公开Schema字段文档不完整")
        if any(doc["database_id"] != self.database or "dense_vector" in doc for doc in documents):
            raise ValueError("公开Schema文档包含其他数据源或预置向量")
        return documents

    def _schema_signature(self) -> str:
        digest = hashlib.sha256(b"askdata_homenova_public_v1")
        for name in ("schema.json", "field_documents.json", "metrics.json"):
            digest.update(name.encode())
            digest.update((homenova.semantic_folder(self.database) / name).read_bytes())
        return digest.hexdigest()

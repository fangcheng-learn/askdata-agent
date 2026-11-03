"""Durable investigation checkpoints and scoped result references.

The store never runs queries or resumes model calls. Callers restore a saved
checkpoint, revalidate current authorization, and explicitly decide how to
handle an interrupted action. Memory can cache these records, but is not their
only copy.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator


class InvestigationRevisionConflict(RuntimeError):
    """A writer used an older task revision and must reload before changing it."""


class InvestigationIntegrityError(RuntimeError):
    """A durable result is incomplete on disk or its immutable content changed."""


class InvestigationStorageError(RuntimeError):
    """A checkpoint cannot be durably saved within the runtime storage quota."""


class InvestigationStore:
    """SQLite-backed task records and immutable, chunked query results.

    Task, owner, session, and data-source bindings are immutable. For concurrent
    task writers, pass the revision returned by ``load_task`` as
    ``expected_revision``. A value of zero creates only; ``None`` permits a
    serialized write by a caller which already owns the task execution lock.
    """

    _CHUNK_ROWS = 500
    DEFAULT_MAX_BYTES = 512 * 1024 * 1024

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if str(path) == ":memory:":
            raise ValueError("调查存储需要持久化 SQLite 文件路径")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("调查存储容量上限必须为正整数字节数")
        self.max_bytes = max_bytes
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def save_task(
        self,
        task_id: str,
        user_id: str,
        session_id: str,
        datasource_id: str,
        payload: dict[str, Any],
        budget: dict[str, Any],
        *,
        results: list[tuple[str, dict[str, Any]]] | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """Commit a task, budget, and optional results in one transaction."""
        self._identity(task_id, "task_id")
        self._identity(user_id, "user_id")
        self._identity(datasource_id, "datasource_id")
        if not isinstance(session_id, str):
            raise ValueError("session_id 必须为字符串")
        if not isinstance(payload, dict) or not isinstance(budget, dict):
            raise ValueError("任务和预算必须为结构化记录")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision 必须为非负整数")
        payload_json, budget_json = self._json(payload), self._json(budget)
        prepared_results = [self._prepare_result(ref, item) for ref, item in results or []]
        for item in prepared_results:
            if item[1:4] != (user_id, task_id, datasource_id):
                raise PermissionError("结果与保存任务的用户、任务或快照不匹配")

        with self._write() as connection:
            previous = connection.execute(
                "SELECT user_id, session_id, datasource_id, revision "
                "FROM investigation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if previous is not None and previous[:3] != (user_id, session_id, datasource_id):
                raise PermissionError("不能更换已保存任务的用户、会话或数据源绑定")
            revision = previous[3] if previous else 0
            if expected_revision is not None and expected_revision != revision:
                raise InvestigationRevisionConflict(
                    f"调查任务修订冲突：期望 {expected_revision}，实际 {revision}"
                )
            for item in prepared_results:
                self._save_prepared_result(connection, item)
            connection.execute(
                """
                INSERT INTO investigation_tasks (
                    task_id, user_id, session_id, datasource_id,
                    payload_json, budget_json, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    budget_json = excluded.budget_json,
                    revision = excluded.revision,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (task_id, user_id, session_id, datasource_id,
                 payload_json, budget_json, revision + 1),
            )

    def load_task(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        self._identity(task_id, "task_id")
        self._identity(user_id, "user_id")
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT task_id, user_id, session_id, datasource_id, "
                "payload_json, budget_json, revision FROM investigation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        if row[1] != user_id:
            raise PermissionError("任务与当前用户不匹配")
        return self._task(row)

    def list_tasks(
        self, user_id: str, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return only this user's checkpoints; loading never resumes them."""
        self._identity(user_id, "user_id")
        query = (
            "SELECT task_id, user_id, session_id, datasource_id, "
            "payload_json, budget_json, revision FROM investigation_tasks WHERE user_id = ?"
        )
        parameters = [user_id]
        if session_id is not None:
            if not isinstance(session_id, str):
                raise ValueError("session_id 必须为字符串")
            query += " AND session_id = ?"
            parameters.append(session_id)
        query += " ORDER BY updated_at DESC, rowid DESC"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._task(row) for row in rows]

    def list_task_summaries(self, user_id: str, datasource_id: str) -> list[dict]:
        """Read sidebar metadata without decoding/restoring full investigation state."""
        self._identity(user_id, 'user_id')
        self._identity(datasource_id, 'datasource_id')
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT task_id, session_id, updated_at, json_extract(payload_json, '$.state.query') "
                "FROM investigation_tasks WHERE user_id=? AND datasource_id=? ORDER BY updated_at DESC",
                (user_id, datasource_id)).fetchall()
        return [dict(task_id=r[0], session_id=r[1], updated_at=r[2], query=r[3] or '') for r in rows]

    def save_result(self, result_ref: str, payload: dict[str, Any]) -> None:
        """Persist all supplied rows without converting a preview to a full result."""
        item = self._prepare_result(result_ref, payload)
        with self._write() as connection:
            self._save_prepared_result(connection, item)

    def task_timestamps(self, user_id: str, datasource_id: str) -> dict[str, str]:
        self._identity(user_id, 'user_id')
        self._identity(datasource_id, 'datasource_id')
        with self._lock, closing(self._connect()) as connection:
            return dict(connection.execute('SELECT task_id,updated_at FROM investigation_tasks '
                'WHERE user_id=? AND datasource_id=?', (user_id, datasource_id)).fetchall())

    def load_result(
        self, ref: str, user_id: str, task_id: str, datasource_id: str
    ) -> dict[str, Any]:
        self._identity(ref, "result_ref")
        for name, value in (("user_id", user_id), ("task_id", task_id),
                            ("datasource_id", datasource_id)):
            self._identity(value, name)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            record = connection.execute(
                "SELECT user_id, task_id, datasource_id, metadata_json, "
                "stored_rows, chunk_count, content_sha256 "
                "FROM investigation_results WHERE result_ref = ?",
                (ref,),
            ).fetchone()
            if record is None:
                raise KeyError(ref)
            if record[:3] != (user_id, task_id, datasource_id):
                raise PermissionError("结果引用与用户、任务或快照不匹配")
            chunks = connection.execute(
                "SELECT chunk_index, rows_json FROM investigation_result_chunks "
                "WHERE result_ref = ? ORDER BY chunk_index",
                (ref,),
            ).fetchall()
            if [item[0] for item in chunks] != list(range(record[5])):
                raise InvestigationIntegrityError("持久化结果数据块缺失")
            payload = json.loads(record[3])
            payload["rows"] = [row for _, rows_json in chunks for row in json.loads(rows_json)]
            if len(payload["rows"]) != record[4] or self._digest(payload) != record[6]:
                raise InvestigationIntegrityError("持久化结果完整性检查失败")
        return payload

    @staticmethod
    def _identity(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须为非空字符串")

    @staticmethod
    def _json(value: Any) -> str:
        # Refuse unsupported values rather than quietly converting evidence or
        # budget objects into non-recoverable string representations.
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))

    @classmethod
    def _digest(cls, payload: dict[str, Any]) -> str:
        return hashlib.sha256(cls._json(payload).encode("utf-8")).hexdigest()

    @classmethod
    def _prepare_result(cls, ref: str, payload: dict[str, Any]) -> tuple:
        cls._identity(ref, "result_ref")
        if not isinstance(payload, dict):
            raise ValueError("结果必须为结构化记录")
        for name in ("user_id", "task_id", "datasource_id"):
            cls._identity(payload.get(name), name)
        rows = payload.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("结果 rows 必须为结构化行列表")
        if not isinstance(payload.get("columns"), list):
            raise ValueError("结果缺少 columns 列表")
        if not isinstance(payload.get("complete"), bool):
            raise ValueError("结果必须声明完整性")
        total = payload.get("total_rows")
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < len(rows)
        ):
            raise ValueError("结果总行数与保存行数不符")
        if payload["complete"] and total != len(rows):
            raise ValueError("完整结果必须包含已声明的全部行，不能只保存预览")
        chunks = [cls._json(rows[index:index + cls._CHUNK_ROWS])
                  for index in range(0, len(rows), cls._CHUNK_ROWS)]
        metadata = {key: value for key, value in payload.items() if key != "rows"}
        return (ref, payload["user_id"], payload["task_id"], payload["datasource_id"],
                cls._json(metadata), len(rows), chunks, cls._digest(payload))

    @staticmethod
    def _save_prepared_result(connection: sqlite3.Connection, item: tuple) -> None:
        ref, user_id, task_id, datasource_id, metadata, count, chunks, digest = item
        previous = connection.execute(
            "SELECT user_id, task_id, datasource_id, content_sha256 "
            "FROM investigation_results WHERE result_ref = ?",
            (ref,),
        ).fetchone()
        if previous is not None:
            if previous[:3] != (user_id, task_id, datasource_id):
                raise PermissionError("不能更换结果引用的用户、任务或快照绑定")
            if previous[3] != digest:
                raise InvestigationIntegrityError("结果引用不可覆盖，修正结果应使用新引用")
            return  # An identical checkpoint save is idempotent.
        connection.execute(
            """
            INSERT INTO investigation_results (
                result_ref, user_id, task_id, datasource_id, metadata_json,
                stored_rows, chunk_count, content_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ref, user_id, task_id, datasource_id, metadata, count, len(chunks), digest),
        )
        connection.executemany(
            "INSERT INTO investigation_result_chunks (result_ref, chunk_index, rows_json) "
            "VALUES (?, ?, ?)",
            [(ref, index, data) for index, data in enumerate(chunks)],
        )

    @staticmethod
    def _task(row: tuple) -> dict[str, Any]:
        return {"task_id": row[0], "user_id": row[1], "session_id": row[2],
                "datasource_id": row[3], "payload": json.loads(row[4]),
                "budget": json.loads(row[5]), "revision": row[6]}

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock, closing(self._connect()) as connection, connection:
            # Locks writers across store instances/processes as well as threads.
            connection.execute("BEGIN IMMEDIATE")
            self._check_capacity(connection, pending_commit=False)
            yield connection
            # Raising here rolls back this entire checkpoint, including any
            # newly inserted result chunks, before the context commits it.
            self._check_capacity(connection, pending_commit=True)

    def _check_capacity(self, connection: sqlite3.Connection, *, pending_commit: bool) -> None:
        """Check actual files and reserve a conservative WAL commit allowance.

        SQLite may buffer dirty pages until commit. Reserving one frame for
        every logical database page bounds a full-page rewrite conservatively;
        it can refuse a save before actual bytes reach the configured limit.
        This is admission control, not an OS filesystem quota: external
        writers and SQLite's temporary/locking behavior are outside this store.
        Existing over-quota records remain readable; no cleanup is automatic.
        """
        sizes = []
        for suffix in ("", "-wal", "-shm"):
            try:
                sizes.append(Path(str(self.path) + suffix).stat().st_size)
            except FileNotFoundError:
                sizes.append(0)
        actual = sum(sizes)
        projected = actual
        if pending_commit:
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            logical_bytes = page_count * page_size
            # Account for a grown main file after checkpoint and the still
            # present WAL/SHM, plus a worst-case newly committed WAL segment.
            projected = max(actual, logical_bytes + sizes[1] + sizes[2])
            projected += 32 + page_count * (page_size + 24)
        if projected > self.max_bytes:
            raise InvestigationStorageError(
                f"调查存储容量不足：现有文件{actual}字节，"
                f"本次保守占用估计{projected}字节，上限{self.max_bytes}字节；"
                "本次保存已停止，已提交记录保留，不自动删除数据"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS investigation_tasks (
                    task_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    datasource_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS idx_investigation_tasks_owner_session
                    ON investigation_tasks (user_id, session_id, updated_at);
                CREATE TABLE IF NOT EXISTS investigation_results (
                    result_ref TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    datasource_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    stored_rows INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investigation_result_chunks (
                    result_ref TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    rows_json TEXT NOT NULL,
                    PRIMARY KEY (result_ref, chunk_index),
                    FOREIGN KEY (result_ref) REFERENCES investigation_results(result_ref)
                        ON DELETE CASCADE
                );
                """
            )

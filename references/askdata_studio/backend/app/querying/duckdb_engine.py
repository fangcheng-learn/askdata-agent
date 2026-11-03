from __future__ import annotations

import re
import math
from collections import defaultdict
from threading import RLock, Timer
import uuid

from ..runtime_budget import current_budget, BudgetStop
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from ..config import BASE_DIR
from ..database import SCHEMA, physical_table_name
from ..security import AccessScope
from ..homenova import is_homenova, schema_payload, snapshot_folder
from .models import SqlExecution


DATABASE_ROOT = BASE_DIR / "data" / "databases"
_HOME_CONNECTIONS: dict[str, Any] = {}
_HOME_LOCKS = defaultdict(RLock)


class DuckDbEngine:
    """将 CSV 目录映射为只读 DuckDB 数据库。

    CSV 文件名作为 SQL 表名使用。
    """

    def __init__(self, database_root: Path | None = None) -> None:
        self.database_root = database_root or DATABASE_ROOT
        self.results: dict[str, dict[str, Any]] = {}
        self.result_store: Any = None

    def execute(
        self,
        database: str,
        sql: str,
        access_scope: AccessScope | None = None,
    ) -> SqlExecution:
        try:
            safe_sql = self._validate_sql(database, sql, access_scope)
            budget = current_budget.get()
            with self.connect(database) as connection:
                if budget:
                    budget.check()
                timer = Timer(budget.remaining(), connection.interrupt) if budget else None
                if timer:
                    timer.start()
                try:
                    cursor = connection.execute(safe_sql)
                    raw_rows = cursor.fetchmany(50001)
                    columns = [item[0] for item in cursor.description or []]
                    if len(columns) != len(set(columns)):
                        raise ValueError("输出列名重复，必须提供唯一别名以免覆盖真实结果")
                    full_rows = [{column: self._json_value(value) for column, value in zip(columns, row)}
                                 for row in raw_rows[:50000]]
                finally:
                    if timer:
                        timer.cancel()
                        timer.join()
            complete = len(raw_rows) <= 50000
            ref = uuid.uuid4().hex
            self.results[ref] = {"user_id": access_scope.user_id if access_scope else None,
                "task_id": budget.task_id if budget else None, "datasource_id": database,
                "sql": safe_sql, "columns": columns, "rows": full_rows, "complete": complete,
                "total_rows": len(full_rows) if complete else None}
            if self.result_store and self.results[ref]['user_id'] and self.results[ref]['task_id']:
                try:
                    self.result_store.save_result(ref, self.results[ref])
                except Exception as exc:
                    self.results.pop(ref, None)
                    return SqlExecution(safe_sql, False, error='结果持久化失败；停止后续执行，不重试SQL',
                                        complete=False, error_kind='storage')
            return SqlExecution(safe_sql, True, columns, full_rows[:200],
                total_rows=len(full_rows) if complete else None, preview_truncated=len(raw_rows)>200,
                complete=complete, result_ref=ref)
        except (ValueError, ParseError, duckdb.Error, OSError) as exc:
            return SqlExecution(sql, False, error=str(exc))

    def read_result(self, ref: str, user_id: str, task_id: str, datasource_id: str,
                    offset: int = 0, limit: int = 200) -> dict[str, Any]:
        item = self.results.get(ref)
        if item is None and self.result_store:
            item = self.result_store.load_result(ref, user_id, task_id, datasource_id)
            self.results[ref] = item
        if not item:
            raise KeyError(ref)
        if (item["user_id"], item["task_id"], item["datasource_id"]) != (user_id, task_id, datasource_id):
            raise PermissionError("结果引用与用户、任务或快照不匹配")
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("结果分页范围无效")
        return {**{key: value for key, value in item.items() if key not in {"rows", "user_id"}},
                "result_ref": ref, "rows": item["rows"][offset:offset+limit], "offset": offset,
                "has_more": offset+limit < len(item["rows"])}

    def clone_result_reference(self, ref: str, user_id: str, source_task_id: str,
                               task_id: str, datasource_id: str) -> str:
        """Bind authorized reused data to a new task without relaxing reads."""
        self.read_result(ref, user_id, source_task_id, datasource_id, 0, 1)
        cloned = uuid.uuid4().hex
        self.results[cloned] = {**self.results[ref], 'task_id': task_id,
                                'rows': [dict(row) for row in self.results[ref]['rows']]}
        if self.result_store:
            self.result_store.save_result(cloned, self.results[cloned])
        return cloned

    def preflight(
        self,
        database: str,
        sql: str,
        access_scope: AccessScope | None = None,
    ) -> str | None:
        """验证只读权限并编译查询，不读取查询结果。"""
        try:
            safe_sql = self._validate_sql(database, sql, access_scope)
            with self.connect(database) as connection:
                budget = current_budget.get()
                if budget:
                    budget.check()
                timer = Timer(budget.remaining(), connection.interrupt) if budget else None
                if timer:
                    timer.start()
                try:
                    connection.execute("EXPLAIN " + safe_sql)
                finally:
                    if timer:
                        timer.cancel()
                        timer.join()
            return None
        except (ValueError, ParseError, duckdb.Error, OSError) as exc:
            return str(exc)

    @staticmethod
    @contextmanager
    def _home_lock(database: str):
        budget = current_budget.get()
        lock = _HOME_LOCKS[database]
        acquired = lock.acquire(timeout=budget.remaining()) if budget else lock.acquire()
        if not acquired:
            raise BudgetStop("等待快照查询资源时主动处理时间预算耗尽")
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def connect(self, database: str) -> Iterator[duckdb.DuckDBPyConnection]:
        """创建内存连接并将 CSV 文件注册为只读视图。"""
        if is_homenova(database):
            with self._home_lock(database):
                connection = _HOME_CONNECTIONS.get(database)
                if connection is None:
                    folder = snapshot_folder(database)
                    tables = schema_payload(database)["tables"]
                    expected = {str(table.get("name") or table["id"]) for table in tables}
                    if {path.stem for path in folder.glob("*.csv")} != expected or len(expected) != 15:
                        raise ValueError("HomeNova快照必须只含公开的十五张业务表")
                    connection = duckdb.connect(":memory:")
                    try:
                        from ..security import public_demo
                        if public_demo.enabled():
                            connection.execute("SET memory_limit='128MB'")
                            connection.execute("SET threads=1")
                            connection.execute("SET max_temp_directory_size='512MB'")
                        for table in tables:
                            name = str(table.get("name") or table["id"])
                            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                                raise ValueError("非法HomeNova表名")
                            types = {}
                            for field in table["fields"]:
                                sql_type = str(field.get("sql_type") or "VARCHAR").upper()
                                if sql_type not in {"BIGINT", "INTEGER", "VARCHAR", "TIMESTAMPTZ", "BOOLEAN", "DATE", "DOUBLE"}:
                                    raise ValueError("未经许可的HomeNova字段类型")
                                types[field["name"]] = sql_type
                            columns = "{" + ", ".join("'" + key.replace("'", "''") + "': '" + value + "'" for key, value in types.items()) + "}"
                            path = (folder / (name + ".csv")).as_posix().replace("'", "''")
                            connection.execute(f'CREATE TABLE "{name}" AS SELECT * FROM read_csv(\'{path}\', header=true, columns={columns}, nullstr=\'\')')
                        connection.execute("SET TimeZone='Europe/Berlin'")
                        connection.execute("SET enable_external_access=false")
                        _HOME_CONNECTIONS[database] = connection
                    except Exception:
                        connection.close()
                        raise
                yield connection
            return
        folder = self._database_folder(database)
        connection = duckdb.connect(":memory:")
        try:
            csv_files = sorted(folder.glob("*.csv"))
            if not csv_files:
                raise ValueError(f"数据库文件夹没有CSV表：{folder}")
            for csv_path in csv_files:
                table = csv_path.stem
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    raise ValueError(f"CSV文件名不能作为安全表名：{csv_path.name}")
                path = csv_path.resolve().as_posix().replace("'", "''")
                connection.execute(
                    f'CREATE VIEW "{table}" AS '
                    f"SELECT * FROM read_csv_auto('{path}', header=true, sample_size=-1)"
                )
            yield connection
        finally:
            connection.close()

    def _validate_sql(
        self,
        database: str,
        sql: str,
        access_scope: AccessScope | None = None,
    ) -> str:
        cleaned = self.clean_sql(sql).rstrip(";").strip()
        statements = [item for item in sqlglot.parse(cleaned, read="duckdb") if item]
        if len(statements) != 1:
            raise ValueError("一次只允许执行一条SQL")
        statement = statements[0]
        if not isinstance(statement, exp.Query):
            raise ValueError("只允许执行SELECT/WITH只读查询")
        if is_homenova(database):
            if re.search(r"\b(current_date|current_timestamp|current_time|localtimestamp|localtime|current_localtimestamp|current_localtime|get_current_timestamp|get_current_time|get_current_date|now|today|transaction_timestamp|statement_timestamp|clock_timestamp)\b", cleaned, re.I):
                raise ValueError("HomeNova禁止使用机器时钟；请使用当前数据源metadata中的场景日期和明确时间边界")
            for table in statement.find_all(exp.Table):
                if table.db or table.catalog:
                    raise ValueError("HomeNova只允许当前数据源未限定库名的公开表，禁止跨库查询")
                if not isinstance(table.this, exp.Identifier):
                    raise ValueError("HomeNova禁止外部文件、网络及动态表函数")
        forbidden_nodes = {
            "insert", "update", "delete", "merge", "create", "drop", "alter",
            "copy", "attach", "detach", "command", "transaction", "grant", "revoke",
        }
        if any(node.key in forbidden_nodes for node in statement.walk()):
            raise ValueError("SQL包含禁止的写入或管理操作")

        # A star over physical data is still rejected. A simple star over an
        # already explicit CTE/subquery can be expanded without adding columns
        # or asking the model to regenerate a valid query.
        expanded = False
        for query_scope in traverse_scope(statement):
            select = query_scope.expression
            if not isinstance(select, exp.Select):
                continue
            projections = []
            for projection in select.expressions:
                if not (isinstance(projection, exp.Star) or isinstance(projection, exp.Column) and projection.is_star):
                    projections.append(projection)
                    continue
                star = projection if isinstance(projection, exp.Star) else projection.this
                selected = [(alias, source) for alias, (_, source) in query_scope.selected_sources.items()
                            if not isinstance(projection, exp.Column) or alias.casefold() == projection.table.casefold()]
                unqualified = isinstance(projection, exp.Star) or not projection.table
                unsafe_join = unqualified and (len(selected) != 1 or bool(select.args.get('joins')))
                if (any(star.args.values()) or not selected or unsafe_join
                        or any(not isinstance(source, Scope) for _, source in selected)
                        or any(node.args.get('recursive') for node in statement.find_all(exp.With))):
                    raise ValueError("不允许使用SELECT *，必须明确列出查询字段")
                for alias, source in selected:
                    outputs = source.outer_columns or [item.alias_or_name for item in source.expression.selects]
                    if not outputs or any(not name or name == '*' for name in outputs) or len({name.casefold() for name in outputs}) != len(outputs):
                        raise ValueError('中间结果的SELECT *无法唯一展开，请明确列出字段')
                    projections.extend(exp.Column(this=exp.Identifier(this=name, quoted=True),
                                                  table=exp.Identifier(this=alias, quoted=True)) for name in outputs)
                expanded = True
            select.set('expressions', projections)
        if expanded:
            cleaned = statement.sql(dialect='duckdb')

        if access_scope and not access_scope.allows_database(database):
            raise ValueError("当前用户无权访问该数据库")

        database_tables = [
            table
            for table in SCHEMA
            if table.get("database", "askdata_mock") == database
        ]
        allowed = {
            physical_table_name(table).casefold(): table["id"]
            for table in database_tables
        }
        referenced = {
            source.name.casefold()
            for query_scope in traverse_scope(statement)
            for source in query_scope.sources.values()
            if isinstance(source, exp.Table)
        }
        unknown = {name for name in referenced if name not in allowed}
        if unknown:
            raise ValueError(f"SQL引用了未知数据表：{', '.join(sorted(unknown))}")
        if access_scope:
            denied = {
                table
                for table in referenced
                if not access_scope.allows_table(database, allowed[table])
            }
            if denied:
                raise ValueError("SQL引用了当前用户无权访问的数据表")
        return cleaned

    def _database_folder(self, database: str) -> Path:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise ValueError("数据库名称不合法")
        folder = (self.database_root / database).resolve()
        root = self.database_root.resolve()
        if root not in folder.parents or not folder.is_dir():
            raise ValueError(f"本地CSV数据库不存在：{database}")
        return folder

    @staticmethod
    def clean_sql(text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:sql)?\s*|\s*```$", "", cleaned, flags=re.I)
        return cleaned.strip()

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            value = float(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("查询返回非有限数值；零分母应使用NULLIF并保留NULL，不能当零或无穷值")
        return value

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..database import SCHEMA
from ..homenova import is_homenova
from . import public_demo


ALL_DATABASES = frozenset(str(table.get("database") or "askdata_mock") for table in SCHEMA)
ALL_TABLES = frozenset(str(table["id"]) for table in SCHEMA)
ECOMMERCE_TABLES = frozenset(
    str(table["id"])
    for table in SCHEMA
    if table.get("database") == "ecommerce_ops"
)
ASKDATA_MOCK_TABLES = frozenset(
    str(table["id"])
    for table in SCHEMA
    if table.get("database", "askdata_mock") == "askdata_mock"
)


@dataclass(frozen=True)
class AccessScope:
    """由后端生成并贯穿整个查询链路的权限范围。"""

    user_id: str
    roles: tuple[str, ...]
    allowed_databases: frozenset[str]
    allowed_tables: frozenset[str]

    def allows_database(self, database: str) -> bool:
        return database in self.allowed_databases

    def allows_table(self, database: str, table_id: str) -> bool:
        return self.allows_database(database) and table_id in self.allowed_tables

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["roles"] = list(self.roles)
        payload["allowed_databases"] = sorted(self.allowed_databases)
        payload["allowed_tables"] = sorted(self.allowed_tables)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> AccessScope:
        raw = payload or {}
        return cls(
            user_id=str(raw.get("user_id") or "anonymous"),
            roles=tuple(str(item) for item in raw.get("roles") or []),
            allowed_databases=frozenset(
                str(item) for item in raw.get("allowed_databases") or []
            ),
            allowed_tables=frozenset(str(item) for item in raw.get("allowed_tables") or []),
        )


class AccessController:
    """根据用户角色返回数据库和表权限。"""

    DEFAULT_USER = "demo_analyst"
    USER_ROLES = {
        "demo_admin": ("admin",),
        "demo_analyst": ("analyst",),
        "demo_current_sales": ("current_sales",),
    }
    ROLE_POLICIES = {
        "admin": {
            "databases": ALL_DATABASES,
            "tables": ALL_TABLES,
        },
        "analyst": {
            "databases": frozenset({"askdata_mock"}),
            "tables": ASKDATA_MOCK_TABLES,
        },
        "current_sales": {
            "databases": frozenset({"ecommerce_ops"}),
            "tables": ECOMMERCE_TABLES,
        },
    }

    def resolve(self, user_id: str | None) -> AccessScope:
        resolved_user = (user_id or self.DEFAULT_USER).strip() or self.DEFAULT_USER
        roles = self.USER_ROLES.get(resolved_user, ())
        databases: set[str] = set()
        tables: set[str] = set()
        for role in roles:
            if role == "visitor":
                if public_demo.enabled():
                    databases.add(public_demo.DATASOURCE)
                    tables.update(str(t['id']) for t in SCHEMA
                                  if t.get('database') == public_demo.DATASOURCE)
                continue
            policy = self.ROLE_POLICIES[role]
            databases.update(policy["databases"])
            tables.update(policy["tables"])
        return AccessScope(
            user_id=resolved_user,
            roles=roles,
            allowed_databases=frozenset(databases),
            allowed_tables=frozenset(tables),
        )

    def scoped(self, user_id: str | None, datasource_id: str | None = None) -> AccessScope:
        scope = self.resolve(user_id)
        if not datasource_id:
            legacy_databases = frozenset(database for database in scope.allowed_databases
                                         if not is_homenova(database))
            return AccessScope(user_id=scope.user_id, roles=scope.roles,
                allowed_databases=legacy_databases,
                allowed_tables=frozenset(table['id'] for table in SCHEMA
                    if table.get('database', 'askdata_mock') in legacy_databases
                    and table['id'] in scope.allowed_tables))
        if not scope.allows_database(datasource_id):
            raise PermissionError("无权访问所选数据源")
        return AccessScope(user_id=scope.user_id, roles=scope.roles,
            allowed_databases=frozenset({datasource_id}),
            allowed_tables=frozenset(table["id"] for table in SCHEMA
                if table.get("database", "askdata_mock") == datasource_id
                and scope.allows_table(datasource_id, table["id"])))

    @staticmethod
    def filter_schema(scope: AccessScope) -> list[dict[str, Any]]:
        return [
            table
            for table in SCHEMA
            if scope.allows_table(
                str(table.get("database") or "askdata_mock"),
                str(table["id"]),
            )
        ]

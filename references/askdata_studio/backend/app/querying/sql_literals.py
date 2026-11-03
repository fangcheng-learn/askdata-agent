"""Interpret a small, deterministic SQL literal subset without executing SQL."""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlglot import exp


def constant_value(node):
    if isinstance(node, exp.Paren):
        return constant_value(node.this)
    if isinstance(node, exp.Cast):
        value = constant_value(node.this)
        target = node.args.get('to')
        if target is None or not isinstance(value, str):
            return value
        kind = target.sql(dialect='duckdb').upper()
        if 'TIMESTAMP' in kind or kind == 'DATE':
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if kind == 'TIMESTAMP':
                    parsed = parsed.replace(tzinfo=None)
                return parsed.date().isoformat() if kind == 'DATE' else parsed.isoformat()
            except ValueError:
                return None
        return value
    if isinstance(node, exp.AtTimeZone):
        value, zone = constant_value(node.this), constant_value(node.args.get('zone'))
        if not isinstance(value, str) or not isinstance(zone, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            tz = ZoneInfo(zone)
            if parsed.tzinfo is None and parsed.replace(tzinfo=tz, fold=0).utcoffset() != parsed.replace(tzinfo=tz, fold=1).utcoffset():
                return None  # DST ambiguity/gap is not a proven constant.
            # DuckDB: naive timestamp -> zoned instant; zoned instant ->
            # local timestamp without timezone. Do not change either meaning.
            return (parsed.replace(tzinfo=tz) if parsed.tzinfo is None else
                    parsed.astimezone(tz).replace(tzinfo=None)).isoformat()
        except (ValueError, ZoneInfoNotFoundError):
            return None
    if isinstance(node, exp.Literal):
        return str(node.this) if node.is_string else int(node.this) if node.is_int else Decimal(node.this)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Neg):
        value = constant_value(node.this)
        return -value if isinstance(value, (int, Decimal)) else None
    return None

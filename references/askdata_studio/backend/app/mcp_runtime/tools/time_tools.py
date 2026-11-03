from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
import re
from pydantic import Field

from ..schemas import DateRangeResult, DateTimeResult
from ...runtime_budget import current_budget


TimezoneName = Literal["Asia/Shanghai", "UTC"]
DateExpression = Annotated[str, Field(pattern=r'^(今天|昨天|本周|上周|本月|上月|今年|(近|最近)[1-9][0-9]{0,3}(天|周))$')]


def _timezone(name: TimezoneName) -> timezone:
    if name == "UTC":
        return timezone.utc
    return timezone(timedelta(hours=8), name="Asia/Shanghai")


def current_datetime(timezone_name: TimezoneName = "Asia/Shanghai") -> DateTimeResult:
    """获取指定时区的当前日期和时间，用于解析今天、当前等相对时间。"""
    budget = current_budget.get()
    now = (datetime.fromisoformat(budget.reference_now).astimezone(_timezone(timezone_name))
           if budget and budget.reference_now else datetime.now(_timezone(timezone_name)))
    return DateTimeResult(
        date=now.date().isoformat(),
        datetime=now.isoformat(timespec="seconds"),
        timezone=timezone_name,
    )


def resolve_date_range(
    expression: DateExpression,
    timezone_name: TimezoneName = "Asia/Shanghai",
) -> DateRangeResult:
    """将常见中文相对时间转换为包含首尾日期的明确日期范围。"""
    today = date.fromisoformat(current_datetime(timezone_name).date)
    start, end = _date_range(expression, today)
    return DateRangeResult(
        expression=expression,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        timezone=timezone_name,
    )


def _date_range(expression: DateExpression, today: date) -> tuple[date, date]:
    recent = re.fullmatch(r'(?:近|最近)([1-9][0-9]{0,3})(天|周)', expression)
    if recent:
        days = int(recent.group(1)) * (7 if recent.group(2) == '周' else 1)
        return today-timedelta(days=days-1), today
    if expression == "今天":
        return today, today
    if expression == "昨天":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if expression == "本周":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if expression == "上周":
        end = today - timedelta(days=today.weekday() + 1)
        return end - timedelta(days=6), end
    if expression == "本月":
        start = today.replace(day=1)
        return start, today.replace(day=monthrange(today.year, today.month)[1])
    if expression == "上月":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if expression == '今年':
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    raise ValueError('不明确或不支持的相对期间；最近多久未说明时应先询问')

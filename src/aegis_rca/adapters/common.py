from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aegis_rca.models.telemetry import ResultMeta

from .http import SchemaError


class CatalogResult(ResultMeta):
    values: tuple[str, ...] = ()


def obj(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise SchemaError(f"malformed {where}: expected object")
    return value


def array(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise SchemaError(f"malformed {where}: expected array")
    return value


def string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"malformed {where}: expected string")
    return value


def strings(value: Any, where: str) -> tuple[str, ...]:
    return tuple(string(item, where) for item in array(value, where))


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = utc(start), utc(end)
    if start >= end:
        raise ValueError("start must precede end; windows use [start, end)")
    return start, end


def iso(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def optional_window(start: datetime | None, end: datetime | None) -> dict[str, str]:
    if (start is None) != (end is None):
        raise ValueError("start and end must be provided together")
    if start is None:
        return {}
    start, end = window(start, end)
    return {"start": iso(start), "end": iso(end)}


def bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ValueError("limit must be an integer in [1, 1000]")
    return value

"""Canonical structural hashing shared by acquisition and verification models."""

from datetime import datetime, timezone
import hashlib
import json


def _canonical(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, float):
        return {"float": (0.0 if value == 0 else value).hex()}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def structural_id(prefix: str, payload: dict) -> str:
    encoded = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()

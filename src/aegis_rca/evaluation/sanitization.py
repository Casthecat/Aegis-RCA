"""Deterministic benchmark export rules; never imported by normalization or policy."""
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from aegis_rca.tools.snapshot import TelemetrySnapshot
from .scoring import scan_leakage

RULESET = "control-metadata-v1"
SCANNER = "existing-heuristic-v1"
SCAN_CAP = 100
MAX_BYTES = 20_000_000
MAX_DEPTH = 64
MAX_NODES = 500_000
DROP = object()


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def check_tree(value: Any) -> None:
    """Fail closed before recursive transformation or path-expanding scanning."""
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("telemetry tree exceeds processing bounds")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)


def control_key(key: Any) -> bool:
    # Explicit semantic namespace, including flattened label spelling.
    return isinstance(key, str) and (key == "feature_flag" or
        key.startswith("feature_flag.") or key.startswith("feature_flag_"))


def _mapping(value: Any) -> dict:
    """Inspect normalized maps and OTLP KeyValue/AnyValue without changing values."""
    if isinstance(value, dict):
        if set(value) == {"kvlistValue"}:
            nested = value["kvlistValue"]
            if not isinstance(nested, dict):
                raise ValueError("malformed OTLP kvlist")
            return _mapping(nested.get("values", []))
        return value
    if isinstance(value, list):
        result = {}
        for pair in value:
            if isinstance(pair, dict) and isinstance(pair.get("key"), str):
                if pair["key"] in result:
                    raise ValueError("ambiguous duplicate OTLP keys")
                result[pair["key"]] = pair.get("value")
        return result
    return {}


def _string(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"stringValue"}:
        return value["stringValue"]
    return value


def control_event(value: Any) -> bool:
    record = _mapping(value)
    attrs = _mapping(record.get("attributes", {}))
    event = _mapping(record.get("event", {}))
    names = (record.get("name"), record.get("event.name"), event.get("name"), attrs.get("event.name"))
    return any(_string(name) == "feature_flag.evaluation" for name in names)


def _clean(value: Any, counts: Counter, manifest: list[dict[str, str]], path: str, *, event_items=False) -> Any:
    if event_items and control_event(value):
        counts["events_removed"] += 1
        manifest.append({"path": path, "rule": "control_event"})
        return DROP
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            # An OTLP KeyValue entry is removed whole, including its AnyValue.
            if isinstance(item, dict) and "value" in item and control_key(item.get("key")):
                counts["attributes_removed"] += 1
                manifest.append({"path": item_path, "rule": "control_attribute"})
                continue
            cleaned = _clean(item, counts, manifest, item_path, event_items=event_items)
            if cleaned is not DROP:
                result.append(cleaned)
        return result
    if isinstance(value, dict):
        result = {}
        # Dynamic keys can themselves contain secrets. Record only their sorted
        # source positions, without changing traversal order or matching rules.
        positions = {key: index for index, key in enumerate(sorted(value))}
        for key, item in value.items():
            item_path = f"{path}{{{positions[key]}}}"
            if control_key(key):
                counts["attributes_removed"] += 1
                manifest.append({"path": item_path, "rule": "control_attribute"})
                continue
            # Retain event context across OTLP array wrappers, not arbitrary objects.
            context = (key == "events" or (key == "value" and value.get("key") == "events") or
                       (event_items and key in {"arrayValue", "values"}))
            cleaned = _clean(item, counts, manifest, item_path, event_items=context)
            if cleaned is not DROP:
                result[key] = cleaned
        return result
    return value


@dataclass(frozen=True)
class SanitizationResult:
    snapshot: TelemetrySnapshot
    removals: dict[str, int]
    removal_manifest: tuple[dict[str, str], ...] = ()


def sanitize_snapshot(snapshot: TelemetrySnapshot) -> SanitizationResult:
    payload = snapshot.model_dump(mode="json")
    check_tree(payload)
    counts = Counter(events_removed=0, attributes_removed=0, logs_removed=0)
    manifest = []
    for index, span in enumerate(payload["traces"]["spans"]):
        span["attributes"] = _clean(span["attributes"], counts, manifest,
                                    f"$.traces.spans[{index}].attributes")
    logs = []
    for index, log in enumerate(payload["logs"]["logs"]):
        attrs = log["attributes"]
        # Body text is NEVER searched/replaced or partially rewritten. Only a
        # complete structured JSON event (or explicit event.name) identifies a log.
        try:
            body = json.loads(log["body"])
            check_tree(body)
        except (ValueError, RecursionError):
            body = None
        if control_event({"attributes": attrs}) or (isinstance(body, dict) and control_event(body)):
            counts["logs_removed"] += 1
            manifest.append({"path": f"$.logs.logs[{index}]", "rule": "control_log"})
            continue
        log["attributes"] = _clean(attrs, counts, manifest, f"$.logs.logs[{index}].attributes")
        logs.append(log)
    payload["logs"]["logs"] = logs
    # Metrics, identity fields, status, scope and ordinary text are not rewritten.
    return SanitizationResult(TelemetrySnapshot.model_validate(payload), dict(counts),
                              tuple(sorted(manifest, key=lambda entry: (entry["path"], entry["rule"]))))


def structural_blockers(snapshot: TelemetrySnapshot) -> list[dict]:
    """Residual explicit control structures; distinct from heuristic text hits."""
    payload = snapshot.model_dump(mode="json")
    check_tree(payload)
    blockers = []
    stack = [("$", payload)]
    # A business log may contain structured control fields without being an
    # evaluation event. Preserve its body exactly and prevent approval instead.
    for index, log in enumerate(snapshot.logs.logs):
        try:
            body = json.loads(log.body)
        except (ValueError, RecursionError):
            continue
        if isinstance(body, (dict, list)):
            try:
                check_tree(body)
            except ValueError:
                blockers.append({"path": f"$.logs.logs[{index}].body", "reason": "structured_body_inspection_incomplete"})
                continue
            stack.append((f"$.logs.logs[{index}].body::<json>", body))
    while stack:
        path, item = stack.pop()
        if isinstance(item, dict):
            if control_event(item):
                blockers.append({"path": path, "reason": "residual_control_event"})
            for key, child in item.items():
                if control_key(key) or (key == "key" and "value" in item and control_key(child)):
                    blockers.append({"path": f"{path}.{key}", "reason": "residual_control_attribute"})
                stack.append((f"{path}.{key}", child))
        elif isinstance(item, (list, tuple)):
            stack.extend((f"{path}[{index}]", child) for index, child in enumerate(item))
    # A feature-flag metric remains intact and cannot be approved by relabeling it.
    for index, series in enumerate(snapshot.metrics.series):
        if control_key(series.name):
            blockers.append({"path": f"$.metrics.series[{index}].name", "reason": "control_metric_identity"})
    return blockers


def scan_payload(payload: Any, *, injected_flags=(), scenario_answers=()) -> dict:
    """Keep the original scanner unchanged; cap exhaustion is conservatively incomplete."""
    try:
        check_tree(payload)
        hits = scan_leakage(payload, injected_flags=injected_flags,
                            scenario_answers=scenario_answers, max_hits=SCAN_CAP)
    except (ValueError, RecursionError, TypeError):
        return {"scanner": SCANNER, "hits": [], "scan_complete": False,
                "scan_truncated": True, "scan_error": "scan_processing_failed", "cap": SCAN_CAP}
    capped = len(hits) >= SCAN_CAP
    return {"scanner": SCANNER, "hits": hits, "scan_complete": not capped,
            "scan_truncated": capped, "scan_error": None, "cap": SCAN_CAP}

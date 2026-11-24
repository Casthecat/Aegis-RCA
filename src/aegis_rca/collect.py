"""Explicit catalog discovery, raw capture and snapshot analysis commands."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cli import load_incident, write_json
from .models.incident import IncidentInput, TimeWindow
from .policy import make_task
from .settings import load_settings
from .tools.live import LiveBackend
from .tools.snapshot import read_snapshot, write_snapshot


def recent_incident(service: str, minutes: int) -> IncidentInput:
    if not 1 <= minutes <= 60:
        raise ValueError("minutes must be between 1 and 60")
    end = datetime.now(timezone.utc)
    return IncidentInput(query=f"Investigate observed errors involving {service}.",
                         seed_services=(service,), window=TimeWindow(start=end - timedelta(minutes=minutes), end=end))


async def collect(args):
    incident = load_incident(args.incident) if args.incident else recent_incident(args.service, args.minutes)
    backend = LiveBackend(load_settings(args.backends), incident)
    try:
        if args.action == "doctor":
            result = await backend.catalog()
            write_json(args.output, result)
            failures = [key for key, value in result.items() if "error" in value]
            print("Backend discovery: " + ("FAILED: " + ", ".join(failures) if failures else "all queries returned"))
            print(f"Catalog: {args.output.resolve()}")
            return 1 if failures else 0
        for signal in ("metric", "trace", "log"):
            result = await backend.query(make_task(incident, signal))
            print(f"{signal}: {result.status}")
        snapshot = backend.snapshot()
        write_snapshot(snapshot, args.output)
        print(f"Snapshot: {args.output.resolve()}")
        return 1 if snapshot.collection_errors else 0
    finally:
        backend.close()


def analyze(args):
    from .analysis import build_timeline, compare_metric, infer_topology
    from .evaluation import scan_leakage
    snapshot = read_snapshot(args.snapshot)
    comparisons = []
    if args.baseline_end:
        split = datetime.fromisoformat(args.baseline_end.replace("Z", "+00:00"))
        baseline = TimeWindow(start=snapshot.incident.window.start, end=split)
        incident = TimeWindow(start=split, end=snapshot.incident.window.end)
        if args.min_absolute_shift is None:
            raise ValueError("Metric analysis needs --min-absolute-shift in the metric's own units")
        comparisons = [compare_metric(series, baseline, incident, min_absolute_shift=args.min_absolute_shift)
                       for series in snapshot.metrics.series]
    write_json(args.output / "metrics.json", [c.model_dump(mode="json") for c in comparisons])
    write_json(args.output / "topology.json", infer_topology(snapshot.traces.spans).model_dump(mode="json"))
    write_json(args.output / "timeline.json", build_timeline(spans=snapshot.traces.spans,
        logs=snapshot.logs.logs, metric_comparisons=comparisons).model_dump(mode="json"))
    hits = scan_leakage(snapshot.model_dump(mode="json"), injected_flags=args.injected_flag)
    write_json(args.output / "leakage-candidates.json", hits)
    print(f"Analysis: {args.output.resolve()} | leakage candidates: {len(hits)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("doctor", "capture"):
        sub = commands.add_parser(action)
        sub.add_argument("--backends", type=Path, required=True)
        sub.add_argument("--incident", type=Path)
        sub.add_argument("--service", default="checkout")
        sub.add_argument("--minutes", type=int, default=10)
        sub.add_argument("--output", type=Path, required=True)
    analysis = commands.add_parser("analyze")
    analysis.add_argument("--snapshot", type=Path, required=True)
    analysis.add_argument("--baseline-end")
    analysis.add_argument("--min-absolute-shift", type=float)
    analysis.add_argument("--injected-flag", action="append", default=[])
    analysis.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "analyze":
            analyze(args)
        else:
            raise SystemExit(asyncio.run(collect(args)))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Input/capture error ({type(exc).__name__}); inspect configuration and output path.\n")


if __name__ == "__main__":
    main()

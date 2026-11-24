import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
from uuid import uuid4

from .fixtures import FixtureBackend, demo_evidence, demo_incident, demo_traces
from .models.incident import Budget, IncidentInput
from .report import to_markdown
from .runner import run_investigation
from .runtime import Dependencies
from .state import initial_state


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def load_incident(path: Path) -> IncidentInput:
    if path.stat().st_size > 65536:
        raise ValueError("Incident file exceeds limit")
    return IncidentInput.model_validate_json(path.read_text(encoding="utf-8-sig"))


def model_runtime(args):
    from .reasoning import BudgetManager, OpenAICompatibleReasoner, ReasoningConfig, ReasoningLimits
    required = ("AEGIS_LLM_ENDPOINT", "AEGIS_LLM_MODEL", "AEGIS_LLM_API_KEY",
                "AEGIS_LLM_INPUT_USD_PER_MILLION", "AEGIS_LLM_OUTPUT_USD_PER_MILLION")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing LLM configuration: " + ", ".join(missing))
    config = ReasoningConfig(endpoint=os.environ[required[0]], model=os.environ[required[1]],
        api_key=os.environ[required[2]], allow_http_loopback=os.environ.get("AEGIS_LLM_ALLOW_HTTP_LOOPBACK") == "true")
    limits = ReasoningLimits(input_usd_per_million=os.environ[required[3]],
        output_usd_per_million=os.environ[required[4]], max_estimated_cost_usd=args.max_llm_cost,
        total_timeout_seconds=args.deadline)
    return OpenAICompatibleReasoner(config, BudgetManager(limits))


async def run(args):
    execution_id = str(uuid4())
    args.output = args.output or Path("artifacts/runs") / execution_id
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve one fresh directory BEFORE network/model work; never mix incidents.
    args.output.mkdir(exist_ok=False)
    from .tools.replay import ReplayBackend
    from .tools.snapshot_v2 import SnapshotV2, load_recording, export_snapshot_v2, write_snapshot_v2
    from .tools.recorded_replay import RecordedReplayBackend
    live = None
    snapshot = None
    if args.mode == "fixture":
        incident = load_incident(args.incident) if args.incident else demo_incident()
        records = demo_evidence()
        if args.scenario == "empty":
            records = ()
        elif args.scenario == "missing-logs":
            records = tuple(e for e in records if e.signal != "log")
        raw_fixture = FixtureBackend()
        backend = FixtureBackend(records, traces=demo_traces() if args.scenario != "empty" else None,
            logs=raw_fixture.logs if args.scenario == "connectivity" else None,
            metrics=raw_fixture.metrics if args.scenario != "empty" else None)
        source_mode = "synthetic_fixture"
    elif args.mode == "replay":
        if not args.snapshot:
            raise ValueError("--snapshot is required for replay")
        snapshot = load_recording(args.snapshot)
        incident = snapshot.incident
        if args.incident and load_incident(args.incident) != incident:
            raise ValueError("Replay incident must match the recorded incident")
        backend = RecordedReplayBackend(snapshot) if isinstance(snapshot, SnapshotV2) else ReplayBackend(snapshot)
        source_mode = "synthetic_fixture" if snapshot.provenance in {"synthetic_wire_fixture", "fixture_investigation"} else "snapshot"
    else:
        from .settings import load_settings
        from .tools.live import LiveBackend
        if not args.backends or not args.incident:
            raise ValueError("--backends and --incident are required for live investigation")
        incident = load_incident(args.incident)
        live = backend = LiveBackend(load_settings(args.backends), incident)
        source_mode = "live"
    try:
        async with AsyncExitStack() as stack:
            reasoner = await stack.enter_async_context(model_runtime(args)) if args.llm else None
            dependencies = Dependencies(backend=backend, reasoner=reasoner)
            state, events = await run_investigation(initial_state(incident,
                Budget(max_tool_calls=args.max_tool_calls, max_rounds=args.max_rounds,
                       tool_timeout_seconds=args.tool_timeout), source_mode=source_mode,
                reasoning_mode="llm_assisted" if reasoner else "deterministic", execution_id=execution_id),
                dependencies, deadline_seconds=args.deadline)
            if reasoner:
                from dataclasses import asdict, is_dataclass
                usage = reasoner.budget.snapshot
                usage = asdict(usage) if is_dataclass(usage) else usage.model_dump(mode="json")
                write_json(args.output / "llm_usage.json", json.loads(json.dumps(usage, default=str)))
        archive = export_snapshot_v2(state, dependencies)
        write_snapshot_v2(archive, args.output / "snapshot.v2.json")
        report = state["final_report"]
        write_json(args.output / "report.json", report.model_dump(mode="json"))
        (args.output / "report.md").write_text(to_markdown(report), encoding="utf-8")
        write_json(args.output / "evidence.json", [e.model_dump(mode="json") for e in state["evidence"]])
        write_json(args.output / "tool_calls.json", [c.model_dump(mode="json") for c in state["tool_calls"]])
        write_json(args.output / "execution.json", {"execution_id": state["execution_id"]})
        write_json(args.output / "intent_ledger.json", [{"id": r.id, **r.model_dump(mode="json")} for r in state["intent_ledger"]])
        write_json(args.output / "scheduling_limitations.json", [r.model_dump(mode="json") for r in state["scheduling_limitations"]])
        write_json(args.output / "task_specs.json", [{"id": t.id, **t.model_dump(mode="json")} for t in state["task_specs"]])
        write_json(args.output / "attempts.json", [{"id": a.id, **a.model_dump(mode="json")} for a in state["attempt_records"]])
        bundle = state["hypothesis_verifications"][0] if len(state["hypothesis_verifications"]) == 1 else None
        write_json(args.output / "connectivity_verification.json", bundle.model_dump(mode="json") if bundle else None)
        portfolio = state["hypothesis_portfolio"]
        write_json(args.output / "hypothesis_portfolio.json", portfolio.model_dump(mode="json") if portfolio else None)
        write_json(args.output / "hypothesis_verifications.json", [b.model_dump(mode="json") for b in state["hypothesis_verifications"]])
        scope = state["investigation_scope"]
        write_json(args.output / "investigation_scope.json", scope.model_dump(mode="json") if scope else None)
        write_json(args.output / "trajectory.json", events)
        if args.mode != "fixture":
            from .analysis import build_timeline, infer_topology
            spans = tuple(s for row in archive.recorded_acquisitions if row.partition and row.partition.signal == "trace"
                          for s in row.partition.material.spans)
            logs = tuple(log for row in archive.recorded_acquisitions if row.partition and row.partition.signal == "log"
                         for log in row.partition.material.logs)
            write_json(args.output / "topology.json", infer_topology(spans).model_dump(mode="json"))
            write_json(args.output / "timeline.json", build_timeline(
                spans=spans, logs=logs).model_dump(mode="json"))
        print(f"{report.status} | {report.rounds} rounds | {report.tool_calls} tool calls | {report.mode}")
        print(report.summary)
        print(f"Report: {(args.output / 'report.md').resolve()}")
    finally:
        if live:
            live.close()


def main():
    parser = argparse.ArgumentParser(description="AegisRCA bounded incident investigation")
    parser.add_argument("--mode", choices=["fixture", "replay", "live"], default="fixture")
    parser.add_argument("--scenario", choices=["connectivity", "missing-logs", "empty"], default="connectivity")
    parser.add_argument("--incident", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--backends", type=Path)
    parser.add_argument("--llm", action="store_true", help="Opt into configured paid/local model calls")
    parser.add_argument("--max-llm-cost", default="0.25")
    parser.add_argument("--deadline", type=float, default=120)
    parser.add_argument("--tool-timeout", type=float, default=5)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--max-tool-calls", type=int, choices=range(0, 101), default=6, metavar="0..100")
    parser.add_argument("--output", type=Path, help="New run directory; default is a fresh artifacts/runs UUID")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        parser.exit(2, f"Configuration/input error ({type(exc).__name__}); check paths, fields and required environment variables.\n")


if __name__ == "__main__":
    main()

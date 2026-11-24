"""Formal Stage C: approved UUIDs only. Engineering snapshot replay is separate."""
import argparse
import asyncio
import json
from pathlib import Path
from .stage_c_models import ExperimentSpec, BenchmarkScoringProtocol
from .stage_c import run_candidate, run_experiment, summarize, read_contract
from .stage_c_scoring import score_run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", allow_abbrev=False)
    run.add_argument("--candidate", required=True)
    run.add_argument("--variant", choices=("B0", "B1", "B2"), default="B2")
    run.add_argument("--trial", type=int, choices=(0,), default=0)
    experiment = commands.add_parser("experiment", allow_abbrev=False)
    experiment.add_argument("--spec", type=Path, required=True)
    for sub in (run, experiment):
        sub.add_argument("--recordings-root", type=Path, default=Path("data/recordings"))
        sub.add_argument("--output", type=Path, required=True)
    score = commands.add_parser("score-run", allow_abbrev=False)
    score.add_argument("--run", type=Path, required=True)
    score.add_argument("--labels-root", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--label-schema", choices=("legacy-v1", "gold-v2"), default="legacy-v1")
    score.add_argument("--scoring-revision")
    score.add_argument("--comparison-run", type=Path)
    summary = commands.add_parser("summarize", allow_abbrev=False)
    summary.add_argument("--experiment", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            value = asyncio.run(run_candidate(args.recordings_root, args.candidate, args.variant,
                trial_index=args.trial, output=args.output))
            result = {"run_key_id": value.run_key_id, "semantic_result_id": value.semantic_result_id,
                      "status": value.semantic.report.status}
        elif args.command == "experiment":
            result = asyncio.run(run_experiment(args.recordings_root, read_contract(args.spec, ExperimentSpec), args.output))
        elif args.command == "score-run":
            if args.label_schema == "gold-v2":
                from .gold_scoring import BenchmarkScoringProtocolV2, score_gold_run
                value = score_gold_run(args.run, args.labels_root, args.output,
                    protocol=BenchmarkScoringProtocolV2(revision=args.scoring_revision or "p2.2-v1"),
                    comparison_path=args.comparison_run)
            else:
                value = score_run(args.run, args.labels_root, args.output,
                    protocol=BenchmarkScoringProtocol(revision=args.scoring_revision or "p2.1-v1"), comparison_path=args.comparison_run)
            result = {"score_id": value.id, **value.model_dump(mode="json")}
        else:
            result = summarize(args.experiment)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except Exception:
        # Never echo payload, private paths, labels or backend exception text.
        parser.exit(2, "Stage C rejected the request or archive; check approval and immutable artifacts.\n")

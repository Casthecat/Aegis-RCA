"""P2.2 operator protocol tooling. Never injects, approves, assigns Gold or runs a pilot automatically."""
import argparse
import asyncio
import json
from pathlib import Path

from .corpus import (CorpusFreezeRequest, freeze_corpus, inspect_corpus_qa, record_holdout_access)
from .corpus_execution import declare_checkpoint, execute_corpus
from .corpus_models import HoldoutAccessEvent
from .corpus_recording import CapturePolicy, PILOT_CAPTURE_POLICY, QUERY_POLICY
from .demo_environment import PILOT_DEMO_RESOURCE_PROFILE
from .gold import DEFAULT_GOLD_RUBRIC, prepare_gold_review, freeze_gold
from .gold_scoring import BenchmarkScoringProtocolV2
from .stage_c import publish, read_contract
from .capture_freeze import freeze_capture_policy
from .corpus_production import update_production


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-pilot", allow_abbrev=False)
    init.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("capture-policy-freeze", allow_abbrev=False)
    for arg in ("policy", "pilot-qa", "decision", "output"):
        capture.add_argument("--"+arg, type=Path, required=True)
    review = commands.add_parser("review-package", allow_abbrev=False)
    review.add_argument("--candidate", required=True)
    review.add_argument("--capture-policy", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    gold = commands.add_parser("gold-freeze", allow_abbrev=False)
    gold.add_argument("--packet", type=Path, required=True)
    gold.add_argument("--decision", type=Path, required=True)
    gold.add_argument("--output", type=Path, required=True)
    production = commands.add_parser("production-update", allow_abbrev=False)
    production.add_argument("--episode", required=True)
    production.add_argument("--candidate")
    production.add_argument("--gold", type=Path)
    production.add_argument("--exclude-reason", action="append", default=[])
    qa = commands.add_parser("qa", allow_abbrev=False)
    freeze = commands.add_parser("freeze", allow_abbrev=False)
    for sub in (qa, freeze):
        sub.add_argument("--request", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
    access = commands.add_parser("access", allow_abbrev=False)
    access.add_argument("--event", type=Path, required=True)
    checkpoint = commands.add_parser("checkpoint", allow_abbrev=False)
    checkpoint.add_argument("--split", action="append", required=True,
        choices=("SMOKE", "DEV", "REPLICATE_HOLDOUT", "FAMILY_HOLDOUT"))
    checkpoint.add_argument("--operator", required=True)
    execute = commands.add_parser("execute", allow_abbrev=False)
    execute.add_argument("--checkpoint", required=True)
    execute.add_argument("--output", type=Path, required=True)
    for sub in (review, gold, qa, freeze, execute, production):
        sub.add_argument("--recordings-root", type=Path, default=Path("data/recordings"))
    for sub in (access, checkpoint, execute):
        sub.add_argument("--corpus", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-pilot":
            if args.output.exists():
                raise ValueError("pilot protocol destination must be new")
            for name, value in (("capture-policy.json", PILOT_CAPTURE_POLICY), ("query-policy.json", QUERY_POLICY),
                ("demo-resource-profile.json", PILOT_DEMO_RESOURCE_PROFILE),
                ("rubric.json", DEFAULT_GOLD_RUBRIC), ("scoring-protocol.json", BenchmarkScoringProtocolV2())):
                publish(args.output / name, value.model_dump(mode="json"))
            result = {"capture_policy_id":PILOT_CAPTURE_POLICY.id, "query_policy_id":QUERY_POLICY.id,
                "demo_resource_profile_id":PILOT_DEMO_RESOURCE_PROFILE.id,
                "gold_diagnostic_rubric_id":DEFAULT_GOLD_RUBRIC.id, "scoring_protocol_id":BenchmarkScoringProtocolV2().id,
                "recordings_started":0}
        elif args.command == "capture-policy-freeze":
            value = freeze_capture_policy(args.policy, args.pilot_qa, args.decision, args.output)
            result = {"capture_policy_freeze_id":value.id, "capture_policy_id":value.policy.id}
        elif args.command == "review-package":
            value = prepare_gold_review(args.recordings_root, args.candidate,
                read_contract(args.capture_policy, CapturePolicy), args.output)
            result = {"review_packet_id":value.id, "candidate_id":value.admission.candidate_id}
        elif args.command == "gold-freeze":
            value = freeze_gold(args.recordings_root, args.packet, args.decision, args.output)
            result = {"gold_label_id":value.id, "candidate_id":value.candidate_id}
        elif args.command == "production-update":
            value = update_production(args.recordings_root, args.episode, candidate_id=args.candidate,
                gold_path=args.gold, exclude_reasons=args.exclude_reason)
            result = {"episode_id":value.private_episode_id, "status":value.status}
        elif args.command in {"qa", "freeze"}:
            request = read_contract(args.request, CorpusFreezeRequest)
            if args.command == "qa":
                value = inspect_corpus_qa(args.recordings_root, request.spec, request.episodes)
                publish(args.output, value.model_dump(mode="json"))
                result = value.model_dump(mode="json")
            else:
                value = freeze_corpus(args.recordings_root, request.spec, request.episodes, args.output)
                result = {"corpus_id":value.execution.corpus_id, "included":len(value.execution.members)}
        elif args.command == "access":
            result = record_holdout_access(args.corpus, read_contract(args.event, HoldoutAccessEvent)).model_dump(mode="json")
        elif args.command == "checkpoint":
            value = declare_checkpoint(args.corpus, args.split, args.operator)
            result = {"checkpoint_id":value.id, **value.model_dump(mode="json")}
        else:
            result = asyncio.run(execute_corpus(args.recordings_root, args.corpus, args.checkpoint, args.output))
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    except Exception:
        parser.exit(2, "Corpus protocol rejected the request; inspect the declared policies and private operator records.\n")

"""Score existing artifacts only: python -m aegis_rca.evaluation score manifest.json."""
import argparse
import json
from pathlib import Path
import sys

from aegis_rca.models.evidence import Evidence
from aegis_rca.models.hypothesis import IncidentReport
from .scoring import HumanReview, Label, Manifest, aggregate, scan_leakage, score_episode, validate_splits


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "corpus":
        from .corpus_cli import main as corpus_main
        return corpus_main(argv[1:])
    if argv and argv[0] in {"run", "experiment", "score-run", "summarize"}:
        from .stage_c_cli import main as stage_c_main
        return stage_c_main(argv)
    if argv and argv[0] in {"sanitize", "scan", "review", "admit"}:
        from .benchmark import main as benchmark_main
        return benchmark_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["score"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        def read(path):
            return json.loads(path.read_text(encoding="utf-8-sig"))
        manifest = Manifest.model_validate(read(args.manifest))
        validate_splits(manifest.episodes)
        base = args.manifest.resolve().parent
        rows, public, input_paths = [], {}, {args.manifest.resolve()}
        for episode in manifest.episodes:
            paths = {key: (base / getattr(episode, key)).resolve() for key in ("report", "evidence", "label")}
            report, evidence = read(paths["report"]), read(paths["evidence"])
            public[str(paths["report"])] = report
            public[str(paths["evidence"])] = evidence
            review = None
            if episode.human_review:
                paths["human_review"] = (base / episode.human_review).resolve()
                review = HumanReview.model_validate(read(paths["human_review"]))
            input_paths.update(paths.values())
            result = score_episode(IncidentReport.model_validate(report), [Evidence.model_validate(item) for item in evidence],
                                   Label.model_validate(read(paths["label"])), episode.window, episode.services,
                                   human_review=review, latency_ms=episode.latency_ms)
            rows.append({"id": episode.id, "group": episode.group, "split": episode.split, **result})
        if args.output.resolve() in input_paths:
            raise ValueError("output must not overwrite an input artifact")
        hits = scan_leakage(public, injected_flags=manifest.injected_flags, scenario_answers=manifest.scenario_answers)
        output = {"notice": "Offline structural scoring of named report records; not a benchmark. Exact mechanism labels and valid citations do not prove semantic support. Grouped/derived records are not independent episodes. Leakage scanning is heuristic and capped at 100 hits.",
                  "episodes": rows, "by_split": {split: aggregate([row for row in rows if row["split"] == split]) for split in ("dev", "validation", "test")},
                  "leakage_hits": hits, "requires_leakage_review": bool(hits)}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

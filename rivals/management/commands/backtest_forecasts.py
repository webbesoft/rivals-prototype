import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from rivals.services.forecast_backtest import load_frames, run_backtest, vaastav_frames


class Command(BaseCommand):
    help = "Evaluate frozen snapshots or lag-only vaastav reconstructions; never fit on held-out seasons"

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--snapshots")
        source.add_argument("--vaastav-root")
        parser.add_argument("--train-seasons", nargs="*", default=[])
        parser.add_argument("--holdout-seasons", nargs="+", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--fit-ridge", action="store_true")
        parser.add_argument("--top-k", type=int, default=40)

    def handle(self, *args, **options):
        try:
            audit = []
            frames = (
                load_frames(options["snapshots"])
                if options["snapshots"]
                else [
                    f
                    for season in options["train_seasons"] + options["holdout_seasons"]
                    for f in vaastav_frames(
                        options["vaastav_root"], season, audit=audit
                    )
                ]
            )
            allowed = set(options["train_seasons"] + options["holdout_seasons"])
            frames = [f for f in frames if f["season"] in allowed]
            if not frames or any(
                not any(f["season"] == season for f in frames) for season in allowed
            ):
                raise ValueError("Every requested season must contain evaluable frames")
            result = run_backtest(
                frames,
                train_seasons=options["train_seasons"],
                holdout_seasons=options["holdout_seasons"],
                fit=options["fit_ridge"],
                top_k=options["top_k"],
            )
            result["coverage_audit"] = audit
            Path(options["output"]).write_text(
                json.dumps(result, indent=2, allow_nan=False) + "\n"
            )
        except (ValueError, KeyError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            json.dumps(
                {
                    "ep_next_status": result["ep_next_status"],
                    "decision_evaluation": result["decision_evaluation"],
                },
                indent=2,
            )
        )

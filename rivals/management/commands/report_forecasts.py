import json
from pathlib import Path

from django.core.management.base import BaseCommand

from rivals.models import GameweekForecast
from rivals.services.forecast_evaluation import evaluate_models, summarise_experiments
from rivals.services.gameweek_forecast_service import EXPERIMENT_VERSION
from rivals.services.forecast_promotion_policy import promotion_policy
from rivals.services.forecast_parameters import model_fingerprint
from rivals.services.calibrated_component_forecast import shadow_fingerprint


class Command(BaseCommand):
    help = "Report saved Best XI accuracy or export pre-deadline inputs plus final labels for backtesting"

    def add_arguments(self, parser):
        parser.add_argument("--season")
        parser.add_argument("--export")
        parser.add_argument("--experiment-version", default=EXPERIMENT_VERSION)
        parser.add_argument("--summary", action="store_true")

    def handle(self, *args, **options):
        records = GameweekForecast.objects.filter(
            version=options["experiment_version"]
        ).order_by("season", "gameweek")
        if options["season"]:
            records = records.filter(season=options["season"])
        if options["summary"]:
            weeks = []
            policy = promotion_policy(options["experiment_version"])
            fingerprint = (
                shadow_fingerprint()
                if options["experiment_version"] == "forecast-experiment-v4"
                else model_fingerprint()
            )
            excluded = []
            for r in records.exclude(outcomes={}):
                if (
                    r.inputs.get("provenance") != "worker-pre-deadline"
                    or r.inputs.get("model_fingerprint") != fingerprint
                    or r.inputs.get("promotion_policy", {}).get("sha256")
                    != policy["sha256"]
                ):
                    excluded.append(
                        {
                            "season": r.season,
                            "gameweek": r.gameweek,
                            "reason": "not current live model/policy cohort",
                        }
                    )
                    continue
                evaluated = evaluate_models(
                    r.inputs["players"],
                    {
                        model: {int(k): v for k, v in values.items()}
                        for model, values in r.predictions.items()
                    },
                    {int(k): v for k, v in r.outcomes["actual"].items()},
                    selections=r.selections,
                    fixtures=r.inputs["fixtures"],
                    week=r.gameweek,
                    frame={
                        **r.inputs,
                        "actual_clean_sheets": r.outcomes.get(
                            "actual_clean_sheets", {}
                        ),
                    },
                )
                weeks.append({"season": r.season, "gameweek": r.gameweek, **evaluated})
            self.stdout.write(
                json.dumps(
                    {
                        "experiment": options["experiment_version"],
                        "completed_gameweeks": len(weeks),
                        "promotion_policy": policy,
                        "model_fingerprint": fingerprint,
                        "excluded_completed_records": excluded,
                        "initial_review_window": "at least 10 completed live gameweeks",
                        "status": (
                            "collecting"
                            if len(weeks) < 10
                            else "initial review available; precision depends on interval width"
                        ),
                        **summarise_experiments(weeks),
                    },
                    indent=2,
                )
            )
        elif options["export"]:
            frames = [
                {
                    **r.inputs,
                    "actual": r.outcomes["actual"],
                    "actual_clean_sheets": r.outcomes.get("actual_clean_sheets", {}),
                }
                for r in records
                if r.outcomes
            ]
            Path(options["export"]).write_text(json.dumps(frames, indent=2) + "\n")
            self.stdout.write(f"Exported {len(frames)} finalised gameweeks")
        else:
            self.stdout.write(
                json.dumps(
                    [
                        {
                            "season": r.season,
                            "gameweek": r.gameweek,
                            "captured_at": r.captured_at.isoformat(),
                            "deadline": r.deadline.isoformat(),
                            "model_fingerprint": r.inputs.get("model_fingerprint"),
                            "history_coverage": r.inputs.get("history_coverage"),
                            "promotion_policy_hash": r.inputs.get(
                                "promotion_policy", {}
                            ).get("sha256"),
                            "selections": r.selections,
                            "outcomes": r.outcomes,
                        }
                        for r in records
                    ],
                    indent=2,
                )
            )

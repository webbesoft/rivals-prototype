import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from rivals.services.component_validation import (
    clean_sheet_validation,
    validate_components,
)
from rivals.services.forecast_backtest import load_frames, vaastav_frames


class Command(BaseCommand):
    help = "Validate fixed component-model ablations on the same forecast-selected candidate pools"

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--snapshots")
        source.add_argument("--vaastav-root")
        parser.add_argument("--development-seasons", nargs="*", default=[])
        parser.add_argument("--evaluation-seasons", nargs="+", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--top-k", nargs="+", type=int, default=[30, 40, 50])

    def handle(self, *args, **options):
        audit, sources = [], []
        try:
            if options["snapshots"]:
                path = Path(options["snapshots"])
                frames = load_frames(path)
                sources.append(
                    {
                        "file": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
            else:
                frames = []
                for season in (
                    options["development_seasons"] + options["evaluation_seasons"]
                ):
                    path = (
                        Path(options["vaastav_root"])
                        / "data"
                        / season
                        / "gws"
                        / "merged_gw.csv"
                    )
                    if not path.exists():
                        path = path.with_name("merged_gws.csv")
                    sources.append(
                        {
                            "season": season,
                            "file": f"data/{season}/gws/{path.name}",
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    )
                    frames.extend(
                        vaastav_frames(options["vaastav_root"], season, audit=audit)
                    )
            result = validate_components(
                frames,
                development_seasons=options["development_seasons"],
                evaluation_seasons=options["evaluation_seasons"],
                top_ks=options["top_k"],
                progress=lambda season, week: self.stderr.write(
                    f"Evaluated {season} GW{week}"
                ),
            )
            protocol_path = (
                Path(__file__).resolve().parents[3]
                / "docs"
                / "component-validation-protocol.json"
            )
            result["registered_protocol"] = {
                **json.loads(protocol_path.read_text()),
                "sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
            }
            declared = result["registered_protocol"]
            result["matches_registered_protocol"] = all(
                sorted(options[option]) == sorted(declared[key])
                for option, key in (
                    ("development_seasons", "development_seasons"),
                    ("evaluation_seasons", "evaluation_seasons"),
                    ("top_k", "top_k"),
                )
            )
            requested = set(
                options["development_seasons"] + options["evaluation_seasons"]
            )
            result["clean_sheet_calibration"] = clean_sheet_validation(
                [f for f in frames if f["season"] in requested]
            )
            result["validation_source_sha256"] = hashlib.sha256(
                (
                    Path(__file__).resolve().parents[2]
                    / "services"
                    / "component_validation.py"
                ).read_bytes()
            ).hexdigest()
            result["requested_splits"] = {
                "development": options["development_seasons"],
                "evaluation": options["evaluation_seasons"],
            }
            result["sources"] = sources
            result["coverage_audit"] = audit
            Path(options["output"]).write_text(
                json.dumps(result, indent=2, allow_nan=False) + "\n"
            )
        except (ValueError, KeyError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Saved component validation to {options['output']}")

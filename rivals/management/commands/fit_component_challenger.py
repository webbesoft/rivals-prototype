import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from rivals.services.component_calibration import (
    revision_predictions,
    select_parameters,
)
from rivals.services.component_validation import validate_components
from rivals.services.forecast_backtest import vaastav_frames
from rivals.services.calibrated_component_forecast import shadow_fingerprint


class Command(BaseCommand):
    help = "Fit the declared v12 fixture and attacker calibration using earlier seasons only"

    def add_arguments(self, parser):
        parser.add_argument("--vaastav-root", required=True)
        parser.add_argument("--parameters-output", required=True)
        parser.add_argument("--selection-output", required=True)
        parser.add_argument("--diagnostics-output")

    def handle(self, *args, **options):
        protocol_path = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "component-calibration-protocol.json"
        )
        protocol = json.loads(protocol_path.read_text())
        try:
            sources, frames, audit = [], [], []
            for season in protocol["final_fit_seasons"]:
                path = (
                    Path(options["vaastav_root"])
                    / "data"
                    / season
                    / "gws"
                    / "merged_gw.csv"
                )
                sources.append(
                    {
                        "season": season,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
                frames.extend(
                    vaastav_frames(options["vaastav_root"], season, audit=audit)
                )
            parameters, selection = select_parameters(
                frames,
                fit_seasons=protocol["fit_seasons"],
                selection_seasons=protocol["selection_seasons"],
                powers=protocol["strength_power_grid"],
                penalty=protocol["penalty"],
                progress=lambda power: self.stderr.write(
                    f"Evaluated fixture strength power {power}"
                ),
            )
            parameters.update(
                protocol_id=protocol["id"],
                sources=sources,
                protocol_sha256=hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
            )
            selection.update(protocol=protocol, sources=sources, coverage_audit=audit)
            Path(options["parameters_output"]).write_text(
                json.dumps(parameters, indent=2, allow_nan=False) + "\n"
            )
            Path(options["selection_output"]).write_text(
                json.dumps(selection, indent=2, allow_nan=False) + "\n"
            )
            if options["diagnostics_output"]:
                diagnostics = []
                diagnostic_audit = []
                for season in protocol["diagnostic_seasons"]:
                    diagnostics.extend(
                        vaastav_frames(
                            options["vaastav_root"], season, audit=diagnostic_audit
                        )
                    )
                result = validate_components(
                    diagnostics,
                    evaluation_seasons=protocol["diagnostic_seasons"],
                    prediction_fn=lambda f: revision_predictions(f, parameters),
                    progress=lambda season, week: self.stderr.write(
                        f"Diagnosed {season} GW{week}"
                    ),
                )
                result["protocol"] = protocol["id"]
                result["comparison_fingerprint"] = result["model_fingerprint"]
                result["model_fingerprint"] = shadow_fingerprint(parameters)
                result["summary"]["diagnostic"] = result["summary"].pop("evaluation")
                result["parameters"] = parameters
                result["coverage_audit"] = diagnostic_audit
                result["note"] = (
                    "Previously inspected diagnostic season; excluded from fitting and grid selection; not a new holdout."
                )
                Path(options["diagnostics_output"]).write_text(
                    json.dumps(result, indent=2, allow_nan=False) + "\n"
                )
        except (ValueError, KeyError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Selected power {parameters['strength_power']}; fitted attacker calibration on {parameters['training_seasons']}"
        )

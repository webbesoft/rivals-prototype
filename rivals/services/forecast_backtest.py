"""Walk-forward evaluation. Labels are never passed to forecast models."""

import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

from .forecast_evaluation import evaluate, evaluate_models, summarise_experiments
from .gameweek_forecast_service import forecasts
from .player_forecast import number
from .forecast_parameters import COMPONENT_CONFIG, model_fingerprint
from .forecast_promotion_policy import promotion_policy
from .historical_forecast_features import (
    recent_play_probability,
    rolling_strengths,
    fixture_proxies,
)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result


def validate_frame(frame):
    captured, deadline = timestamp(frame["captured_at"]), timestamp(frame["deadline"])
    if captured >= deadline:
        raise ValueError("Forecast inputs must have been captured before the deadline")
    for rows in frame.get("histories", {}).values():
        for row in rows:
            if timestamp(row["kickoff_time"]) >= captured:
                raise ValueError("Player history contains a future match")
    if frame["scoring_gameweek"] >= frame["gameweek"]:
        raise ValueError("Scoring gameweek must precede the forecast gameweek")
    ids = [p["id"] for p in frame["players"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate player IDs")


def run_backtest(frames, *, train_seasons=(), holdout_seasons=(), fit=False, top_k=40):
    if set(train_seasons) & set(holdout_seasons):
        raise ValueError("Training and held-out seasons must be disjoint")
    if train_seasons and holdout_seasons and max(train_seasons) >= min(holdout_seasons):
        raise ValueError("Training seasons must precede held-out seasons")
    from .forecast_ridge import fit_ridge, predict_ridge

    frames = list(frames)
    for frame in frames:
        validate_frame(frame)
    artifact = None
    if fit:
        training = [f for f in frames if f["season"] in train_seasons]
        if not training or not holdout_seasons:
            raise ValueError(
                "Fitting requires explicit earlier training and held-out seasons"
            )
        artifact = fit_ridge(training)
    results, seen = [], set()
    for frame in sorted(frames, key=lambda f: (f["season"], f["gameweek"])):
        validate_frame(frame)
        identity = (frame["season"], frame["gameweek"])
        if identity in seen:
            raise ValueError("Use one pre-deadline snapshot per season/gameweek")
        seen.add(identity)
        actual = {int(k): number(v, float("nan")) for k, v in frame["actual"].items()}
        if any(not math.isfinite(v) for v in actual.values()):
            raise ValueError("Non-finite or missing actual scores")
        predictions = forecasts(frame)
        if artifact:
            predictions[artifact["version"]] = predict_ridge(frame, artifact)
        evaluated = evaluate_models(
            frame["players"],
            predictions,
            actual,
            fixtures=frame["fixtures"],
            week=frame["gameweek"],
            top_k=top_k,
            frame=frame,
        )
        metrics = evaluated["metrics"]
        # ep_next can be missing: compare every model on the exact common cohort too.
        common = set.intersection(*(set(values) for values in predictions.values()))
        common_metrics = {
            model: evaluate(
                frame["players"],
                {pid: value for pid, value in values.items() if pid in common},
                actual,
            )
            for model, values in predictions.items()
        }
        results.append(
            {
                "season": frame["season"],
                "gameweek": frame["gameweek"],
                "split": (
                    "holdout"
                    if frame["season"] in holdout_seasons
                    else "train" if frame["season"] in train_seasons else "evaluation"
                ),
                "provenance": frame.get("provenance", "provided-snapshot"),
                "metrics": metrics,
                "common_cohort_metrics": common_metrics,
                "paired_differences": evaluated["paired_differences"],
                "paired_history_differences": evaluated["paired_history_differences"],
            }
        )
    summaries = {}
    for split in sorted({r["split"] for r in results}):
        summaries[split] = {}
        models = {
            model for r in results if r["split"] == split for model in r["metrics"]
        }
        for model in sorted(models):
            rows = [
                r["metrics"][model]
                for r in results
                if r["split"] == split and model in r["metrics"]
            ]
            n = sum(r["players"] for r in rows)
            summaries[split][model] = {
                "gameweeks": len(rows),
                "player_gameweeks": n,
                "mae": sum(r["mae"] * r["players"] for r in rows) / n,
                "rmse": (sum(r["rmse"] ** 2 * r["players"] for r in rows) / n) ** 0.5,
                "bias": sum(r["bias"] * r["players"] for r in rows) / n,
                "mean_xi_actual": (
                    mean(r["xi_actual"] for r in rows if "xi_actual" in r)
                    if any("xi_actual" in r for r in rows)
                    else None
                ),
                "mean_captain_actual": (
                    mean(r["captain_actual"] for r in rows if "captain_actual" in r)
                    if any("captain_actual" in r for r in rows)
                    else None
                ),
            }
    return {
        "model_config": dict(COMPONENT_CONFIG),
        "model_fingerprint": model_fingerprint(),
        "promotion_policy": promotion_policy(),
        "promotion_eligibility": "development only: historical frames do not count as live gameweeks",
        "summary": summaries,
        "decision_evaluation": {
            split: summarise_experiments([r for r in results if r["split"] == split])
            for split in summaries
        },
        "top_k": top_k,
        "ep_next_status": (
            "evaluated on supplied pre-deadline coverage"
            if any("ep_next" in r["metrics"] for r in results)
            else "untested: no trustworthy pre-deadline ep_next"
        ),
        "weeks": results,
        "ridge_artifact": artifact,
        "rules": "Ridge fits training seasons only when requested; other models are fixed. Models see inputs only. Hindsight XI uses same eligible pool, no budget cap, three-per-club, captain doubled.",
    }


def load_frames(path):
    path = Path(path)
    if path.suffix == ".jsonl":
        return [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
    return json.loads(path.read_text())


def vaastav_frames(root, season, *, audit=None):
    """Lag-only reconstruction, explicitly NOT an exact pre-deadline replay.

    Never read players_raw season totals or xP. Pool/positions/club identities come
    from prior GW rows only; fixtures are reconstructed final schedules, a known
    limitation. A player is introduced only after their first observed GW.
    """
    directory = Path(root) / "data" / season
    path = directory / "gws" / "merged_gw.csv"
    if not path.exists():
        path = directory / "gws" / "merged_gws.csv"
    with path.open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    by_week = defaultdict(list)
    for row in rows:
        by_week[int(row["GW"])].append(row)
    histories = defaultdict(list)
    positions = {"GK": 1, "GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}
    frames = []
    audit = audit if audit is not None else []
    for week, current in sorted(by_week.items()):
        kicks = [timestamp(r["kickoff_time"]) for r in current if r.get("kickoff_time")]
        if not kicks:
            audit.append(
                {
                    "season": season,
                    "gameweek": week,
                    "status": "skipped",
                    "reason": "no_kickoff_times",
                }
            )
            continue
        # Deadline proxy is documented; exact archives must supply actual deadlines.
        deadline = min(kicks) - timedelta(minutes=90)
        captured = deadline - timedelta(seconds=1)
        players, history_payload = [], {}
        for pid, old_rows in sorted(histories.items()):
            available = [
                r
                for r in old_rows
                if timestamp(r["kickoff_time"]) + timedelta(hours=3) < captured
            ]
            if not available:
                continue
            latest = available[-1]
            total_minutes = sum(number(r.get("minutes")) for r in available)
            points = sum(number(r.get("total_points")) for r in available)
            appearances = sum(number(r.get("minutes")) > 0 for r in available)
            last_weeks = [r for r in available if int(r["GW"]) >= week - 5]
            form = sum(number(r.get("total_points")) for r in last_weeks) / min(
                5, week - 1
            )
            p = {
                "id": pid,
                "web_name": latest.get("name", str(pid)),
                "element_type": positions.get(latest.get("position"), 0),
                "team": latest.get("team"),
                "minutes": total_minutes,
                "points_per_game": points / max(1, appearances),
                "form": form,
                "status": "a",
                "chance_of_playing_next_round": 100
                * recent_play_probability(available),
                "availability_source": "recent-match-proxy",
            }
            if p["element_type"] == 0 or not p["team"]:
                continue
            for field in ("expected_goals", "expected_assists"):
                if any(r.get(field) not in (None, "") for r in available):
                    p[field] = sum(number(r.get(field)) for r in available)
            players.append(p)
            history_payload[pid] = [
                {**r, "fixture": int(r.get("fixture") or 0)} for r in available[-6:]
            ]
        fixtures, seen = [], set()
        for row in current:
            fixture_id = row.get("fixture")
            if not fixture_id or fixture_id in seen:
                continue
            team = row.get("team")
            # Resolve opponent identity from the opposing row in this match.
            other = next(
                (
                    r.get("team")
                    for r in current
                    if r.get("fixture") == fixture_id and r.get("team") != team
                ),
                None,
            )
            if not team or not other:
                continue
            seen.add(fixture_id)
            home = str(row.get("was_home")).lower() == "true"
            fixtures.append(
                {
                    "id": int(fixture_id),
                    "event": week,
                    "team_h": team if home else other,
                    "team_a": other if home else team,
                    "team_h_difficulty": 3,
                    "team_a_difficulty": 3,
                    "finished": False,
                }
            )
        actual = defaultdict(float)
        for row in current:
            actual[int(row["element"])] += number(row.get("total_points"))
        prior_rows = [
            r
            for old_rows in histories.values()
            for r in old_rows
            if timestamp(r["kickoff_time"]) + timedelta(hours=3) < captured
        ]
        clubs = {p["team"] for p in players} | {
            f[key] for f in fixtures for key in ("team_h", "team_a")
        }
        teams = rolling_strengths(prior_rows, clubs)
        fixtures = fixture_proxies(fixtures, teams)
        missing = [p["id"] for p in players if p["id"] not in actual]
        audit.append(
            {
                "season": season,
                "gameweek": week,
                "phase": "early" if week <= 5 else "established",
                "status": "evaluated" if players and not missing else "skipped",
                "reason": (
                    None
                    if players and not missing
                    else "no_prior_player_pool" if not players else "missing_labels"
                ),
                "known_players": len(players),
                "missing_label_count": len(missing),
                "missing_player_ids": missing,
            }
        )
        cs_rows = defaultdict(list)
        for row in current:
            cs_rows[int(row["element"])].append(row)
        cs_labels = {
            pid: sum(
                (
                    number(row.get("clean_sheets"))
                    if number(row.get("minutes")) >= 60
                    else 0
                )
                for row in labels
            )
            for pid, labels in cs_rows.items()
            if all(row.get("clean_sheets") not in (None, "") for row in labels)
        }
        # Absent labels are not assumed zero: incomplete weeks are skipped.
        if players and not missing:
            frames.append(
                {
                    "season": season,
                    "gameweek": week,
                    "captured_at": captured.isoformat(),
                    "deadline": deadline.isoformat(),
                    "scoring_gameweek": week - 1,
                    "players": players,
                    "fixtures": fixtures,
                    "teams": teams,
                    "histories": history_payload,
                    "actual": dict(actual),
                    "actual_clean_sheets": cs_labels,
                    "provenance": "vaastav-v2-rolling-team-goals-shared-availability-proxy-no-ep_next",
                }
            )
        for row in current:
            if row.get("kickoff_time"):
                histories[int(row["element"])].append(row)
        for pid in histories:
            histories[pid].sort(key=lambda r: r["kickoff_time"])
    for absent in sorted(set(range(1, 39)) - set(by_week)):
        audit.append(
            {
                "season": season,
                "gameweek": absent,
                "status": "skipped",
                "reason": "absent_from_archive",
                "phase": "early" if absent <= 5 else "established",
            }
        )
    return frames

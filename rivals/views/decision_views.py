import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from rivals.forms import DecisionInputForm
from rivals.services.ai_credentials import configured
from rivals.models import DecisionRecord, TeamPlanningInput
from rivals.services.decision_engine import DecisionEngine, DecisionUnavailable, digest

logger = logging.getLogger(__name__)


def report_context(report, user):
    for option in report["options"]:
        option["save_token"] = signing.dumps(
            {
                "user": user.pk,
                "team": user.team_id,
                "fingerprint": report["fingerprint"],
                "option": option["key"],
            },
            salt="decision",
        )
    return {
        "report": report,
        "decision_deadline": parse_datetime(report["deadline"]),
        "decision_prepared": parse_datetime(report["created_at"]),
    }


def financial_defaults(engine, data):
    record = engine.saved
    result = {}
    for field in ("free_transfers", "bank"):
        timestamp = f"{field}_recorded_at"
        if field in data["financial_overrides"]:
            result[field] = data["financial_overrides"][field]
            result[timestamp] = timezone.now()
        elif record and getattr(record, field) is not None:
            result[field] = getattr(record, field)
            result[timestamp] = getattr(record, timestamp) or record.updated_at
    prices = dict(record.selling_prices) if record else {}
    markets = dict(record.selling_price_markets) if record else {}
    for pid, price in data["price_overrides"].items():
        prices[pid] = price
        markets[pid] = engine.players[int(pid)]["now_cost"]
    result["selling_prices"] = {
        pid: price for pid, price in prices.items() if int(pid) in data["ids"]
    }
    result["selling_price_markets"] = {
        pid: value for pid, value in markets.items() if pid in result["selling_prices"]
    }
    return result


@login_required
@never_cache
def planner(request):
    return render(request, "pages/decisions.html")


@login_required
@require_GET
@never_cache
def summary(request):
    try:
        report = DecisionEngine(request.user).report()
        return render(
            request,
            "partials/decision_summary.html",
            report_context(report, request.user),
        )
    except (DecisionUnavailable, ValueError) as error:
        return render(
            request, "partials/decision_summary.html", {"decision_error": str(error)}
        )
    except Exception:
        logger.exception("Decision summary unavailable for user %s", request.user.pk)
        return render(
            request,
            "partials/decision_summary.html",
            {
                "decision_error": "Your transfer outlook is temporarily unavailable. Please try again."
            },
        )


@login_required
@never_cache
def workspace(request):
    if request.method not in ("GET", "POST"):
        return HttpResponseBadRequest("Use the transfer planner to review your squad.")
    try:
        engine = DecisionEngine(request.user).load()
        form = DecisionInputForm(
            request.POST if request.method == "POST" else None, engine=engine
        )
        report = None
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            inputs = {
                "ids": data["ids"],
                "sales": data["sales"],
                "confirmed": data["squad_confirmed"],
                "bank": int(data["bank"] * 10) if data["bank"] is not None else None,
                "free_transfers": data["free_transfers"],
                "horizon": data["horizon"],
                "strategy": data["strategy"],
                "league_id": (
                    data["target_league"].pk if data["target_league"] else None
                ),
                "rival_id": data["target_rival"].pk if data["target_rival"] else None,
                "manual": data["manual"],
            }
            try:
                report = engine.report(inputs)
            except ValueError as error:
                form.add_error(None, str(error))
            else:
                TeamPlanningInput.objects.update_or_create(
                    user=request.user,
                    team=request.user.team,
                    season=engine.context["season"],
                    gameweek=engine.context["decision_gameweek"],
                    defaults={
                        **financial_defaults(engine, data),
                        "squad_ids": data["ids"],
                        "squad_confirmed": data["squad_confirmed"],
                        "horizon": data["horizon"],
                        "strategy": data["strategy"],
                        "target_league": data["target_league"],
                        "target_rival": data["target_rival"],
                        "manual_moves": data["manual"],
                    },
                )
        elif request.method == "GET":
            report = engine.report()
        context = {"form": form, "ai_enabled": configured()}
        if report:
            context.update(report_context(report, request.user))
        return render(request, "partials/decision_workspace.html", context)
    except DecisionUnavailable as error:
        return render(
            request, "partials/decision_workspace.html", {"decision_error": str(error)}
        )
    except Exception:
        logger.exception("Decision workspace unavailable for user %s", request.user.pk)
        return render(
            request,
            "partials/decision_workspace.html",
            {"decision_error": "We couldn't prepare your plan. Please try again."},
        )


@login_required
@require_POST
@never_cache
def save_decision(request):
    try:
        token = signing.loads(
            request.POST.get("choice", ""), salt="decision", max_age=3600
        )
        if token["user"] != request.user.pk or token["team"] != request.user.team_id:
            raise signing.BadSignature("Different manager")
        report = DecisionEngine(request.user).report()
        if report["fingerprint"] != token["fingerprint"]:
            raise ValueError(
                "Your options have changed. Review your latest plan before saving."
            )
        option = next(
            (item for item in report["options"] if item["key"] == token["option"]), None
        )
        if not option:
            raise ValueError("Review your latest options before saving.")
        DecisionRecord.objects.get_or_create(
            user=request.user,
            fingerprint=digest([report["fingerprint"], option["key"]]),
            defaults={
                "team": request.user.team,
                "season": report["season"],
                "gameweek": report["gameweek"],
                "snapshot": {"report": report, "chosen": option},
            },
        )
        messages.success(request, "Plan saved to your decision journal.")
    except (signing.BadSignature, ValueError, KeyError) as error:
        messages.error(
            request,
            (
                str(error)
                if isinstance(error, ValueError)
                and not isinstance(error, signing.BadSignature)
                else "Please review your plan again before saving."
            ),
        )
    except Exception:
        logger.exception("Could not save decision for user %s", request.user.pk)
        messages.error(request, "We couldn't save your plan. Please try again.")
    return redirect("rivals:decisions.journal")


@login_required
@never_cache
def journal(request):
    rows = Paginator(DecisionRecord.objects.filter(user=request.user), 10).get_page(
        request.GET.get("page")
    )
    return render(request, "pages/decision_journal.html", {"decisions": rows})


@login_required
@require_POST
def journal_status(request, decision_id):
    record = get_object_or_404(DecisionRecord, pk=decision_id, user=request.user)
    status = request.POST.get("status")
    if status in {"planned", "acted", "held"}:
        record.status = status
        record.save(update_fields=["status"])
    return redirect("rivals:decisions.journal")

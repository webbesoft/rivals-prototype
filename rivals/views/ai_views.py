from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.ai_privacy import sensitive_ai
from rivals.services.ai_briefing_service import current_run, request_briefing
from rivals.services.ai_credentials import AIUnavailable, configured
from rivals.services.decision_engine import DecisionEngine


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
@sensitive_ai
def briefing(request):
    fingerprint = (request.POST if request.method == "POST" else request.GET).get(
        "fingerprint", ""
    )[:64]
    error = ""
    if request.method == "POST":
        try:
            report = DecisionEngine(request.user).report()
            if fingerprint != report["fingerprint"]:
                raise AIUnavailable(
                    "Your plan has changed. Reload it before preparing a new briefing."
                )
            request_briefing(request.user, report)
        except AIUnavailable as exception:
            error = str(exception)
        except Exception:
            error = "We couldn't prepare your Gemini briefing. Your transfer advice is still available."
    run, state = current_run(request.user, fingerprint)
    cards = []
    if run and state == "ready":
        by_id = {item["id"]: item for item in run.evidence}
        cards = [by_id[pid] for pid in run.priorities if pid in by_id]
    return render(
        request,
        "partials/ai_briefing.html",
        {
            "ai_fingerprint": fingerprint,
            "ai_state": state,
            "ai_cards": cards,
            "ai_error": error,
            "ai_configured": configured(),
        },
    )

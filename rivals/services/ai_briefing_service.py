"""Per-manager reservations, revocable work and evidence-bound briefings."""

from background_task import background
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from core.ai_privacy import sensitive_ai
from rivals.models import AIBriefing, AICredential, AIUsage, AIProviderCall
from rivals.services.ai_credentials import AIUnavailable, configured, decrypt
from rivals.services.decision_engine import DecisionEngine
from rivals.services.gemini_service import MODEL, TOKEN_RESERVATION, Gemini
from rivals.services.ai_usage_audit import event, run_event, prepare_call, enabled


def evidence_for(report):
    """No account/league identifiers or raw news. All displayed facts are engine-owned."""
    best = report["best"]
    horizon = report["horizon"]
    if best["moves"]:
        title = "; ".join(f'{m["out_name"]} → {m["in_name"]}' for m in best["moves"])
        detail = (
            f'An estimated {best["net_gain"]:+.1f} points versus holding over {horizon} gameweeks, '
            f'after a {best["hits"]}-point hit. Estimated bank remaining: £{best["bank_after"]:.1f}m.'
        )
    else:
        title = "Keep your squad for now"
        detail = "No available route clearly beats holding after estimated transfer costs. Keep your options open for later news."
    evidence = [{"id": "route", "title": title, "detail": detail}]
    if report["captain"]:
        evidence.append(
            {
                "id": "captain",
                "title": f'Captain {report["captain"]}',
                "detail": f'Vice-captain: {report["vice"]}. These choices use the next gameweek projection; check availability near the deadline.',
            }
        )
    if report["risks"]:
        names = ", ".join(risk["name"] for risk in report["risks"])
        evidence.append(
            {
                "id": "availability",
                "title": "Watch availability and minutes",
                "detail": f"{names}: availability or playing-time uncertainty affects the forecast. Later team news could change the plan.",
            }
        )
    if best.get("free_next") is not None:
        evidence.append(
            {
                "id": "flexibility",
                "title": "Keep next week's flexibility in view",
                "detail": f'This route leaves an estimated {best["free_next"]} free transfers next gameweek under the normal allowance. The forecast assumes no later moves or chips.',
            }
        )
    if best["moves"]:
        evidence.append(
            {
                "id": "uncertainty",
                "title": "Treat the gain as an estimate",
                "detail": f'The sensitivity range versus holding is {best["gain_low"]} to {best["gain_high"]} points. This is not a guaranteed return or a probability interval.',
            }
        )
    rival = report.get("rival")
    if rival and not rival.get("unavailable") and best.get("vs_rival") is not None:
        evidence.append(
            {
                "id": "rival",
                "title": "Compare with your rival's unchanged squad",
                "detail": f'This route projects a {best["vs_rival"]:+.1f}-point difference over {horizon} gameweeks if your rival keeps their published squad. Their next transfers and captain choice remain unknown.',
            }
        )
    for player in report["players"]:
        first = player["schedule"][0]
        opponents = first["opponents"]
        if len(opponents) != 1:
            label = "a blank" if not opponents else "multiple fixtures"
            evidence.append(
                {
                    "id": f'fixture_{player["id"]}',
                    "title": f'{player["name"]} has {label}',
                    "detail": f'GW{first["gameweek"]}: {", ".join(opponents) or "no scheduled fixture"}. This is already included in the squad forecast.',
                }
            )
        if len(evidence) >= 10:
            break
    return evidence


def request_briefing(user, report, *, enqueue=True, trigger="planner", digest_id=None):
    event("requested", user_id=user.pk, trigger=trigger, digest_id=digest_id)
    try:
        return _request_briefing(
            user, report, enqueue=enqueue, trigger=trigger, digest_id=digest_id
        )
    except AIUnavailable as error:
        event(
            "denied",
            user_id=user.pk,
            trigger=trigger,
            digest_id=digest_id,
            reason=error.code,
        )
        raise


def _request_briefing(user, report, *, enqueue, trigger, digest_id):
    if not enabled():
        raise AIUnavailable(
            "AI generation is paused. Your structured advice is still available.",
            code="disabled",
        )
    if (
        not user.is_verified
        or not user.is_active
        or (user.team_id and not user.team.active)
    ):
        raise AIUnavailable(
            "Verify your email before preparing an AI briefing.", code="not_eligible"
        )
    if not configured():
        raise AIUnavailable(
            "AI briefings are temporarily unavailable. Your transfer advice is still available."
        )
    deadline = parse_datetime(report["deadline"])
    now = timezone.now()
    if not user.team_id or not deadline or deadline <= now:
        raise AIUnavailable("Reload your plan to see the next deadline.")
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        if AIProviderCall.objects.filter(
            user=user, usage_exceeds_reservation=True, reviewed_at__isnull=True
        ).exists():
            raise AIUnavailable(
                "AI generation is paused pending a usage review. Your structured advice is still available.",
                code="usage_bounds",
            )
        credential = AICredential.objects.filter(
            user=user, verified_at__isnull=False
        ).first()
        if not credential:
            raise AIUnavailable(
                "Enable AI briefings in your account settings first.",
                code="no_credential",
            )
        existing = (
            AIBriefing.objects.filter(
                user=user,
                team=user.team,
                credential_revision=credential.revision,
                fingerprint=report["fingerprint"],
                expires_at__gt=now,
                created_at__gte=now - timezone.timedelta(minutes=10),
                status__in=["queued", "running", "ready"],
            )
            .order_by("-created_at")
            .first()
        )
        if existing:
            event("reused", run=existing, trigger=trigger, digest_id=digest_id)
            return existing
        recent = AIBriefing.objects.filter(
            user=user, created_at__gt=now - timezone.timedelta(minutes=10)
        ).exists()
        usage, _ = AIUsage.objects.get_or_create(user=user, date=now.date())
        usage = AIUsage.objects.select_for_update().get(pk=usage.pk)
        if (
            recent
            or usage.generations >= 6
            or usage.reserved_tokens + TOKEN_RESERVATION > 100000
        ):
            raise AIUnavailable(
                "Your AI briefing limit has been reached. Your transfer advice is still available.",
                code="cooldown" if recent else "daily_limit",
            )
        reserved = (
            AIUsage.objects.filter(
                pk=usage.pk,
                generations__lt=6,
                reserved_tokens__lte=100000 - TOKEN_RESERVATION,
            )
            .filter(
                Q(last_generation_at__isnull=True)
                | Q(last_generation_at__lte=now - timezone.timedelta(minutes=10))
            )
            .update(
                generations=F("generations") + 1,
                reserved_tokens=F("reserved_tokens") + TOKEN_RESERVATION,
                last_generation_at=now,
            )
        )
        if not reserved:
            usage.refresh_from_db()
            raise AIUnavailable(
                "Your AI briefing limit has been reached. Your transfer advice is still available.",
                code=(
                    "cooldown"
                    if usage.last_generation_at
                    and usage.last_generation_at > now - timezone.timedelta(minutes=10)
                    else "daily_limit"
                ),
            )
        run = AIBriefing.objects.create(
            user=user,
            team=user.team,
            credential_revision=credential.revision,
            fingerprint=report["fingerprint"],
            model=MODEL,
            trigger=trigger,
            source_digest_id=digest_id,
            evidence=evidence_for(report),
            expires_at=min(deadline, now + timezone.timedelta(minutes=30)),
        )
        run_event("reserved", run, reserved_tokens=TOKEN_RESERVATION)
        # Only the reference is serialised into the queue, never a key or a prompt.
        if enqueue:
            prepare_briefing(str(run.pk))
        return run


def current_run(user, fingerprint):
    credential = AICredential.objects.filter(user=user).values("revision").first()
    if not credential or not user.team_id:
        return None, "disconnected"
    run = (
        AIBriefing.objects.filter(
            user=user, team_id=user.team_id, credential_revision=credential["revision"]
        )
        .order_by("-created_at")
        .first()
    )
    if not run:
        return None, "idle"
    if run.expires_at <= timezone.now() or run.fingerprint != fingerprint:
        return None, "stale"
    if run.status in (
        "queued",
        "running",
    ) and run.created_at < timezone.now() - timezone.timedelta(minutes=10):
        return None, "delayed"
    return run, run.status


@background(schedule=0)
@sensitive_ai
@sensitive_variables()
def prepare_briefing(run_id):
    complete_briefing(run_id)


@sensitive_ai
@sensitive_variables()
def complete_briefing(run_id, report=None):
    """Execute once, optionally using a digest's freshly computed planner snapshot."""
    # Duplicate/retried jobs cannot cause another provider request.
    claimed = AIBriefing.objects.filter(pk=run_id, status="queued").update(
        status="running", started_at=timezone.now()
    )
    if not claimed:
        run = AIBriefing.objects.filter(pk=run_id).first()
        if run:
            run_event("duplicate_skipped", run)
        return
    run = AIBriefing.objects.select_related("user", "team").get(pk=run_id)
    run_event("claimed", run)
    call = None
    try:
        credential = AICredential.objects.filter(
            user=run.user, revision=run.credential_revision
        ).first()
        if (
            not credential
            or not run.user.is_verified
            or not run.user.is_active
            or not run.team.active
            or run.user.team_id != run.team_id
            or run.expires_at <= timezone.now()
            or run.created_at < timezone.now() - timezone.timedelta(minutes=10)
        ):
            AIBriefing.objects.filter(pk=run_id, status="running").update(
                status="cancelled", evidence=[], priorities=[]
            )
            run_event(
                "cancelled",
                run,
                reason="expired" if run.expires_at <= timezone.now() else "revoked",
            )
            return
        report = report if report is not None else DecisionEngine(run.user).report()
        if report["fingerprint"] != run.fingerprint:
            AIBriefing.objects.filter(pk=run_id, status="running").update(
                status="stale", evidence=[], priorities=[]
            )
            run_event("stale", run, reason="plan_changed")
            return
        # Re-check revocation after potentially slow source reads.
        if not AICredential.objects.filter(
            user=run.user, revision=run.credential_revision
        ).exists():
            run_event("cancelled", run, reason="revoked")
            return
        call = prepare_call(run, TOKEN_RESERVATION)
        priorities, input_tokens, output_tokens = Gemini(call_id=call.pk).prioritise(
            decrypt(credential), run.evidence, report["strategy"]
        )
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=run.user_id)
            if (
                AICredential.objects.filter(
                    user=run.user, revision=run.credential_revision
                ).exists()
                and run.expires_at > timezone.now()
            ):
                AIBriefing.objects.filter(pk=run_id, status="running").update(
                    status="ready",
                    priorities=priorities,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                run_event("result_ready", run, call=call)
            else:
                run_event("result_withheld", run, call=call, reason="revoked")
    except Exception as error:
        # No provider exceptions, prompts, keys or raw output reach worker logs/task errors.
        AIBriefing.objects.filter(pk=run_id, status="running").update(
            status="failed", error_code="unavailable"
        )
        run_event(
            "failed",
            run,
            call=call,
            reason=error.code if isinstance(error, AIUnavailable) else "unavailable",
        )

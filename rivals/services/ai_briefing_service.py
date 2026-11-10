"""Per-manager reservations, revocable work and evidence-bound briefings."""

from background_task import background
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from core.ai_privacy import sensitive_ai
from rivals.models import AIBriefing, AICredential, AIUsage
from rivals.services.ai_credentials import AIUnavailable, configured, decrypt
from rivals.services.decision_engine import DecisionEngine
from rivals.services.gemini_service import MODEL, TOKEN_RESERVATION, Gemini


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


def request_briefing(user, report):
    if not user.is_verified:
        raise AIUnavailable("Verify your email before preparing an AI briefing.")
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
        credential = AICredential.objects.filter(
            user=user, verified_at__isnull=False
        ).first()
        if not credential:
            raise AIUnavailable("Enable AI briefings in your account settings first.")
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
                "Your AI briefing limit has been reached. Your transfer advice is still available."
            )
        usage.generations += 1
        usage.reserved_tokens += TOKEN_RESERVATION
        usage.save(update_fields=["generations", "reserved_tokens"])
        run = AIBriefing.objects.create(
            user=user,
            team=user.team,
            credential_revision=credential.revision,
            fingerprint=report["fingerprint"],
            model=MODEL,
            evidence=evidence_for(report),
            expires_at=min(deadline, now + timezone.timedelta(minutes=30)),
        )
        # Only the reference is serialised into the queue, never a key or a prompt.
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
    # Duplicate/retried jobs cannot cause another provider request.
    claimed = AIBriefing.objects.filter(pk=run_id, status="queued").update(
        status="running", started_at=timezone.now()
    )
    if not claimed:
        return
    run = AIBriefing.objects.select_related("user", "team").get(pk=run_id)
    try:
        credential = AICredential.objects.filter(
            user=run.user, revision=run.credential_revision
        ).first()
        if (
            not credential
            or not run.user.is_verified
            or run.user.team_id != run.team_id
            or run.expires_at <= timezone.now()
            or run.created_at < timezone.now() - timezone.timedelta(minutes=10)
        ):
            AIBriefing.objects.filter(pk=run_id, status="running").update(
                status="cancelled", evidence=[], priorities=[]
            )
            return
        report = DecisionEngine(run.user).report()
        if report["fingerprint"] != run.fingerprint:
            AIBriefing.objects.filter(pk=run_id, status="running").update(
                status="stale", evidence=[], priorities=[]
            )
            return
        # Re-check revocation after potentially slow source reads.
        if not AICredential.objects.filter(
            user=run.user, revision=run.credential_revision
        ).exists():
            return
        priorities, input_tokens, output_tokens = Gemini().prioritise(
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
    except Exception:
        # No provider exceptions, prompts, keys or raw output reach worker logs/task errors.
        AIBriefing.objects.filter(pk=run_id, status="running").update(
            status="failed", error_code="unavailable"
        )

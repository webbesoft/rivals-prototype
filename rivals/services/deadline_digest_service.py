"""Deadline-aware, consented email work with durable generation and send claims."""

from datetime import timedelta

from background_task import background
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from core.ai_privacy import sensitive_ai
from rivals.models import AICredential, DeadlineDigest, DigestSubscription
from rivals.services.ai_briefing_service import complete_briefing, request_briefing
from rivals.services.ai_credentials import AIUnavailable
from rivals.services.decision_engine import DecisionEngine, DecisionUnavailable
from rivals.services.fpl_api_service import FplApiService
from rivals.services.gameweek_context_service import GameweekContextService
from rivals.services.planner_presentation import planner_pitch_weeks
from rivals.services.team_sync_service import TeamSyncService

LEASE = timedelta(minutes=10)
GRACE = timedelta(minutes=30)
MAX_ATTEMPTS = 3


def eligible(user):
    return bool(
        user.is_active and user.is_verified and user.team_id and user.team.active
    )


def consented(user):
    return DigestSubscription.objects.filter(
        user=user, enabled=True, paused=False, consented_at__isnull=False
    ).exists()


def unsubscribe_url(user):
    subscription, _ = DigestSubscription.objects.get_or_create(user=user)
    token = signing.dumps(
        {"user": user.pk, "revision": str(subscription.unsubscribe_token)},
        salt="deadline-unsubscribe",
    )
    return settings.SITE_URL.rstrip("/") + reverse(
        "accounts:digests.unsubscribe", args=[token]
    )


def skip(record, notice):
    # Never rewrite a send with an unknown outcome or a completed delivery.
    DeadlineDigest.objects.filter(pk=record.pk, generation=record.generation).exclude(
        delivery_state__in=["sending", "sent", "ambiguous"]
    ).update(
        generation_state="skipped",
        delivery_state="skipped",
        notice=notice,
        lease_until=None,
    )


def dispatch_digests():
    """Called by the existing minute dispatcher; never makes HTTP requests."""
    now = timezone.now()
    DeadlineDigest.objects.filter(
        delivery_state="sending", lease_until__lte=now
    ).update(
        delivery_state="ambiguous",
        lease_until=None,
        notice="Delivery outcome is unknown; automatic resend is disabled.",
    )
    DeadlineDigest.objects.filter(
        generation_state__in=["queued", "generating"],
        lease_until__lte=now,
    ).update(generation_state="pending", lease_until=None)
    DeadlineDigest.objects.filter(
        delivery_state="connecting", lease_until__lte=now
    ).update(
        delivery_state="pending",
        lease_until=None,
    )
    for record in DeadlineDigest.objects.filter(deadline__lte=now).exclude(
        delivery_state__in=["sent", "ambiguous", "sending", "skipped"]
    ):
        skip(record, "Deadline passed; obsolete advice was not sent.")

    context = GameweekContextService.cached()
    if context["stale"] or not context["next_deadline"]:
        return
    deadline = context["next_deadline"]
    subscriptions = DigestSubscription.objects.select_related("user__team").filter(
        enabled=True,
        paused=False,
        consented_at__isnull=False,
        user__is_active=True,
        user__is_verified=True,
        user__team__active=True,
    )
    for subscription in subscriptions:
        due = deadline - timedelta(hours=subscription.lead_hours)
        record, _ = DeadlineDigest.objects.get_or_create(
            user=subscription.user,
            season=context["season"],
            gameweek=context["decision_gameweek"],
            kind="scheduled",
            defaults={
                "team": subscription.user.team,
                "deadline": deadline,
                "scheduled_for": due,
            },
        )
        if record.delivery_state in ("sent", "sending", "ambiguous"):
            continue
        # A changed deadline/lead time invalidates any generated forecast and old job.
        if (
            record.deadline != deadline
            or record.scheduled_for != due
            or record.team_id != subscription.user.team_id
        ):
            DeadlineDigest.objects.filter(pk=record.pk).exclude(
                delivery_state__in=["sent", "sending", "ambiguous"]
            ).update(
                team=subscription.user.team,
                deadline=deadline,
                scheduled_for=due,
                generation_state="pending",
                delivery_state="pending",
                generation=F("generation") + 1,
                attempts=0,
                delivery_attempts=0,
                report={},
                notice="",
                lease_until=None,
                next_attempt_at=None,
            )

    pending = (
        DeadlineDigest.objects.select_related("user__team")
        .filter(
            deadline__gt=now,
            scheduled_for__lte=now,
        )
        .exclude(delivery_state__in=["sent", "sending", "ambiguous", "skipped"])
    )
    for record in pending:
        if not eligible(record.user) or record.user.team_id != record.team_id:
            skip(record, "Account or linked team changed.")
            continue
        if record.kind == "scheduled" and not consented(record.user):
            skip(record, "Deadline digests are paused or disabled.")
            continue
        if record.kind == "scheduled" and now - record.scheduled_for > GRACE:
            skip(record, "The delivery window was missed; no late digest was sent.")
            continue
        if record.kind != "scheduled" and now - record.created_at > GRACE:
            skip(record, "This preview or test expired. Request a new one.")
            continue
        if record.next_attempt_at and record.next_attempt_at > now:
            continue
        if (
            record.generation_state in ("pending", "failed")
            and record.attempts < MAX_ATTEMPTS
        ):
            with transaction.atomic():
                claimed = DeadlineDigest.objects.filter(
                    pk=record.pk,
                    generation=record.generation,
                    generation_state__in=["pending", "failed"],
                ).update(
                    generation_state="queued",
                    generation=F("generation") + 1,
                    lease_until=now + LEASE,
                )
                if claimed:
                    generate_digest(str(record.pk), record.generation + 1, priority=-5)
        elif (
            record.generation_state == "ready"
            and record.delivery_state == "pending"
            and record.delivery_attempts < MAX_ATTEMPTS
            and record.kind != "preview"
        ):
            deliver_digest(str(record.pk), priority=-5)


def request_digest(user, kind):
    """Preview and test are explicit, rate-limited requests; they never opt in."""
    if kind not in ("preview", "test") or not eligible(user):
        raise ValueError("Verify your email and link an active FPL team first.")
    context = GameweekContextService.cached()
    if context["stale"] or not context["next_deadline"]:
        raise ValueError(
            "The next deadline is being refreshed. Please try again shortly."
        )
    now = timezone.now()
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        recent = DeadlineDigest.objects.filter(
            user=user, kind=kind, created_at__gte=now - timedelta(days=1)
        )
        if (
            recent.count() >= 3
            or recent.filter(created_at__gte=now - timedelta(minutes=2)).exists()
        ):
            raise ValueError(
                "Please wait before requesting another preview or test. Up to three of each are available per day."
            )
        record = DeadlineDigest.objects.create(
            user=user,
            team=user.team,
            season=context["season"],
            gameweek=context["decision_gameweek"],
            kind=kind,
            deadline=context["next_deadline"],
            scheduled_for=now,
            generation_state="queued",
            generation=1,
            lease_until=now + LEASE,
        )
        generate_digest(str(record.pk), 1, priority=-5)
    return record


def fresh_report(user):
    """Refresh public financial/history inputs, and bypass old stored lineup rows."""
    api = FplApiService()
    api.TIMEOUT = 4
    bootstrap = api.fetch_bootstrap_static(force_refresh=True)
    fixtures = api.fetch_fixtures_data(force_refresh=True)
    if not bootstrap or fixtures is None:
        raise DecisionUnavailable("Fresh player and fixture details are unavailable.")
    context = GameweekContextService.derive(bootstrap.get("events", []))
    if not context["season"] or not context["decision_gameweek"]:
        raise DecisionUnavailable("The next deadline is unavailable.")
    team = user.team
    profile = api.fetch_team_basic_info(team.fpl_team_id)
    history = api.fetch_team_history(team.fpl_team_id)
    transfers = api.fetch_team_transfers(team.fpl_team_id)
    if not profile or not history or "current" not in history or transfers is None:
        raise DecisionUnavailable("Fresh squad inputs are unavailable.")
    service = TeamSyncService(team)
    service.fpl_api_service = api
    service._history_data = history
    with transaction.atomic():
        service.detect_and_handle_season_transition(live_season=context["season"])
        if (
            not service.sync_basic_info(data=profile)
            or service.sync_history(include_picks=False) is None
            or not service.sync_transfers(
                transfers=transfers, sync_missing_players=False
            )
        ):
            raise DecisionUnavailable("Fresh squad inputs could not be saved.")

    # The engine reads precisely the bootstrap/fixture snapshot just fetched.
    class SnapshotAPI:
        TIMEOUT = 4

        def fetch_bootstrap_static(self):
            return bootstrap

        def fetch_fixtures_data(self):
            return fixtures

        def __getattr__(self, name):
            return getattr(api, name)

    return DecisionEngine(user, api=SnapshotAPI(), fresh_squad=True).report()


@background(schedule=0)
@sensitive_ai
@sensitive_variables()
def generate_digest(record_id, generation):
    now = timezone.now()
    claimed = DeadlineDigest.objects.filter(
        pk=record_id,
        generation=generation,
        generation_state="queued",
        attempts__lt=MAX_ATTEMPTS,
    ).update(
        generation_state="generating",
        attempts=F("attempts") + 1,
        lease_until=now + LEASE,
    )
    if not claimed:
        return
    record = DeadlineDigest.objects.select_related("user__team").get(pk=record_id)
    if (
        not eligible(record.user)
        or record.user.team_id != record.team_id
        or (record.kind == "scheduled" and not consented(record.user))
        or record.deadline <= now
    ):
        skip(record, "This digest is no longer eligible for delivery.")
        return
    try:
        report = fresh_report(record.user)
        deadline = parse_datetime(report["deadline"])
        if (
            report["season"] != record.season
            or report["gameweek"] != record.gameweek
            or deadline <= timezone.now()
        ):
            skip(record, "The gameweek changed; obsolete advice was not sent.")
            return
        if deadline != record.deadline:
            subscription = DigestSubscription.objects.filter(user=record.user).first()
            due = (
                deadline
                - timedelta(hours=subscription.lead_hours if subscription else 4)
                if record.kind == "scheduled"
                else timezone.now()
            )
            DeadlineDigest.objects.filter(
                pk=record.pk, generation=generation, generation_state="generating"
            ).update(
                deadline=deadline,
                scheduled_for=due,
                generation_state="pending",
                attempts=0,
                lease_until=None,
                notice="The FPL deadline changed; delivery has been rescheduled.",
            )
            return
        # One reserved provider run per digest. A recovered job reuses that run,
        # including a failed/ambiguous one, instead of making another paid request.
        record.user = (
            get_user_model().objects.select_related("team").get(pk=record.user_id)
        )
        if (
            not eligible(record.user)
            or record.user.team_id != record.team_id
            or (record.kind == "scheduled" and not consented(record.user))
            or not DeadlineDigest.objects.filter(
                pk=record.pk, generation=generation, generation_state="generating"
            ).exists()
        ):
            skip(record, "This briefing was cancelled while its inputs refreshed.")
            return
        run = record.ai_briefing
        if not run:
            try:
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=record.user_id)
                    if record.kind == "scheduled" and not consented(record.user):
                        skip(
                            record, "This briefing was cancelled before AI preparation."
                        )
                        return
                    try:
                        run = request_briefing(
                            record.user,
                            report,
                            enqueue=False,
                            trigger=record.kind,
                            digest_id=record.pk,
                        )
                    except AIUnavailable:
                        # Commit the denial audit instead of rolling it back with this scope.
                        run = None
                    if run is None:
                        linked = True
                    else:
                        linked = DeadlineDigest.objects.filter(
                            pk=record.pk,
                            generation=generation,
                            generation_state="generating",
                        ).update(ai_briefing=run)
                    if not linked:
                        return
                if run is not None:
                    complete_briefing(str(run.pk), report=report)
            except Exception:
                # The structured briefing remains deliverable with no provider/key.
                pass
        DeadlineDigest.objects.filter(
            pk=record.pk, generation=generation, generation_state="generating"
        ).update(
            generation_state="ready",
            prepared_at=timezone.now(),
            report=report,
            notice="",
            lease_until=None,
            next_attempt_at=None,
        )
        if record.kind != "preview":
            deliver_digest(record_id, priority=-5)
    except Exception:
        # Generic bounded notices; no provider output, private inputs or SMTP errors.
        DeadlineDigest.objects.filter(
            pk=record.pk, generation=generation, generation_state="generating"
        ).update(
            generation_state="failed",
            lease_until=None,
            next_attempt_at=timezone.now() + timedelta(minutes=record.attempts * 2),
            notice="Fresh advice could not be generated. Open the planner for the latest available advice.",
        )


def email_context(record):
    report = record.report
    weeks = planner_pitch_weeks(report) if report else []
    outlook = []
    for week, lineup in zip(weeks, report.get("baseline", {}).get("lineups", [])):
        players = [player for row in week["rows"] for player in row["players"]] + week[
            "bench"
        ]
        outlook.append(
            {
                "gameweek": week["gameweek"],
                "points": lineup["points"],
                "blanks": sum(not p["fixture"]["opponents"] for p in players),
                "doubles": sum(len(p["fixture"]["opponents"]) > 1 for p in players),
                "risks": sum(bool(p["risk_label"]) for p in players),
            }
        )
    cards = []
    run = record.ai_briefing
    if (
        run
        and run.user_id == record.user_id
        and run.team_id == record.team_id
        and run.status == "ready"
        and run.fingerprint == report.get("fingerprint")
        and run.expires_at > timezone.now()
        and AICredential.objects.filter(
            user_id=record.user_id, revision=run.credential_revision
        ).exists()
    ):
        by_id = {item["id"]: item for item in run.evidence}
        cards = [by_id[key] for key in run.priorities if key in by_id]
    return {
        "report": report,
        "first_week": weeks[0] if weeks else None,
        "outlook": outlook,
        "deadline": record.deadline,
        "prepared_at": record.prepared_at,
        "planner_url": settings.SITE_URL.rstrip("/") + reverse("rivals:decisions"),
        "settings_url": settings.SITE_URL.rstrip("/") + reverse("accounts:digests"),
        "unsubscribe_url": unsubscribe_url(record.user),
        "is_test": record.kind == "test",
        "notice": record.notice,
        "ai_cards": cards,
        "ai_notice": (
            ""
            if cards
            else "AI priorities are unavailable for this briefing. Your forecast and structured planner advice are included below."
        ),
    }


def send_eligible(record):
    return (
        eligible(record.user)
        and record.user.team_id == record.team_id
        and record.deadline > timezone.now()
        and record.prepared_at
        and timezone.now() - record.prepared_at < timedelta(minutes=10)
        and (
            record.kind != "scheduled"
            or (
                consented(record.user)
                and timezone.now() - record.scheduled_for <= GRACE
            )
        )
    )


@background(schedule=0)
@sensitive_ai
@sensitive_variables()
def deliver_digest(record_id):
    now = timezone.now()
    claimed = (
        DeadlineDigest.objects.filter(
            pk=record_id,
            generation_state="ready",
            delivery_state="pending",
            delivery_attempts__lt=MAX_ATTEMPTS,
        )
        .exclude(kind="preview")
        .update(
            delivery_state="connecting",
            delivery_attempts=F("delivery_attempts") + 1,
            lease_until=now + LEASE,
        )
    )
    if not claimed:
        return
    record = DeadlineDigest.objects.select_related("user__team").get(pk=record_id)
    if not send_eligible(record):
        skip(
            record,
            "Delivery was cancelled or the advice expired. Open the planner for fresh advice.",
        )
        return
    connection = None
    try:
        # Recheck the actual event deadline before submitting an email.
        api = FplApiService()
        api.TIMEOUT = 4
        bootstrap = api.fetch_bootstrap_static(force_refresh=True)
        event = next(
            (
                event
                for event in (bootstrap or {}).get("events", [])
                if event["id"] == record.gameweek
            ),
            None,
        )
        live = GameweekContextService.derive((bootstrap or {}).get("events", []))
        if not event or not live["season"]:
            raise DecisionUnavailable("Deadline unavailable")
        if (
            live["season"] != record.season
            or parse_datetime(event["deadline_time"]) != record.deadline
        ):
            skip(
                record,
                "The deadline changed. A new forecast is required before delivery.",
            )
            return
        # A connection failure happens before message submission and is safe to retry.
        connection = get_connection(fail_silently=False, timeout=15)
        connection.open()
        with transaction.atomic():
            # Settings updates use the same user lock. Recheck consent/team/email after slow reads.
            user = (
                get_user_model()
                .objects.select_for_update()
                .select_related("team")
                .get(pk=record.user_id)
            )
            record.user = user
            if not send_eligible(record):
                skip(record, "Delivery was cancelled before sending.")
                return
            record.refresh_from_db(fields=["ai_briefing"])
            context = email_context(record)
            message = EmailMultiAlternatives(
                ("[Test] " if record.kind == "test" else "")
                + f"GW{record.gameweek}: your AI briefing",
                render_to_string("emails/deadline_digest.txt", context),
                settings.DEFAULT_FROM_EMAIL,
                [record.user.email],
                headers={
                    "Message-ID": f"<{record.pk}@rivalspy.digest>",
                    "List-Unsubscribe": f'<{context["unsubscribe_url"]}>',
                },
            )
            message.attach_alternative(
                render_to_string("emails/deadline_digest.html", context), "text/html"
            )
            message.connection = connection
            claimed = DeadlineDigest.objects.filter(
                pk=record.pk,
                generation=record.generation,
                delivery_state="connecting",
            ).update(
                delivery_state="sending",
                recipient=user.email,
                lease_until=timezone.now() + LEASE,
            )
            if not claimed:
                return
            message.to = [user.email]
        try:
            sent = message.send()
        except Exception:
            sent = 0
        # SMTP may have accepted DATA before the connection was lost. Never retry it.
        DeadlineDigest.objects.filter(pk=record.pk, delivery_state="sending").update(
            delivery_state="sent" if sent == 1 else "ambiguous",
            lease_until=None,
            sent_at=timezone.now() if sent == 1 else None,
            notice=(
                ""
                if sent == 1
                else "Delivery outcome is unknown; automatic resend is disabled."
            ),
        )
    except Exception:
        DeadlineDigest.objects.filter(
            pk=record.pk, generation=record.generation, delivery_state="connecting"
        ).update(
            delivery_state="pending",
            lease_until=None,
            next_attempt_at=timezone.now()
            + timedelta(minutes=record.delivery_attempts * 2),
            notice="Email connection failed before sending. Delivery will be retried within this deadline window.",
        )
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass

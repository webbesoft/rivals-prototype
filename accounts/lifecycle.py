"""Account deletion and the seven-day FPL onboarding grace period."""

import json
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def onboarding_deadline():
    return timezone.now() + timedelta(days=7)


def _delete_account_tasks(user_id):
    # Legacy initialization jobs contain FPL profile snapshots rather than FKs.
    from background_task.models import CompletedTask, Task

    names = (
        "accounts.tasks.init_user_data_task",
        "accounts.tasks.send_verification_email_task",
    )
    for model in (Task, CompletedTask):
        jobs = model.objects.filter(task_name__in=names).values_list(
            "pk", "task_params"
        )
        for pk, params in jobs.iterator(chunk_size=100):
            args, kwargs = json.loads(params)
            owner = args[0] if args else kwargs.get("user_id")
            if str(owner) == str(user_id):
                model.objects.filter(pk=pk).delete()


def _delete_locked_account(user):
    from .models import SignInLink

    # Challenges are addressed by email, so Django's FK cascades cannot find them.
    emails = {user.email, *user.emailaddress_set.values_list("email", flat=True)}
    for email in emails:
        SignInLink.objects.filter(email__iexact=email).delete()
    _delete_account_tasks(user.pk)
    # Cascades remove private plans, credentials, digests, social accounts and
    # API/OAuth grants. Shared public FPL teams/leagues remain available to others.
    user.delete()


def delete_account(user_id):
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().filter(pk=user_id).first()
        if user is None:
            return False
        _delete_locked_account(user)
    return True


def delete_expired_accounts(*, now=None, limit=100, dry_run=False):
    """Recheck eligibility under the same user lock used to publish FPL setup."""
    from .onboarding import has_fpl_setup

    now = now or timezone.now()
    User = get_user_model()
    candidates = list(
        User.objects.filter(
            onboarding_expires_at__lte=now, is_staff=False, is_superuser=False
        )
        .order_by("onboarding_expires_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    deleted = 0
    for user_id in candidates:
        with transaction.atomic():
            user = User.objects.select_for_update().filter(pk=user_id).first()
            if (
                user is None
                or user.is_staff
                or user.is_superuser
                or user.onboarding_expires_at is None
                or user.onboarding_expires_at > now
            ):
                continue
            if has_fpl_setup(user):
                if not dry_run:
                    User.objects.filter(pk=user.pk).update(onboarding_expires_at=None)
                continue
            if not dry_run:
                _delete_locked_account(user)
            deleted += 1
    if deleted and not dry_run:
        logger.info("Deleted %s accounts with expired FPL onboarding", deleted)
    return deleted

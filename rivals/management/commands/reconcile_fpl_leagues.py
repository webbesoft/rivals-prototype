from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from rivals.services.fpl_api_service import FplApiService
from rivals.services.league_membership_service import (
    account_leagues,
    reconcile_memberships,
)


class Command(BaseCommand):
    help = "Compare saved memberships with FPL; dry run unless --apply is given"

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--retire-legacy",
            action="store_true",
            help="Also deactivate missing legacy memberships after reviewing the dry run",
        )

    def handle(self, *args, **options):
        user = (
            get_user_model()
            .objects.filter(pk=options["user_id"])
            .select_related("team")
            .first()
        )
        if not user or not user.team:
            raise CommandError("User must have a linked FPL team")
        api = FplApiService()
        api.TIMEOUT = 5
        profile = api.fetch_team_basic_info(user.team.fpl_team_id)
        try:
            current = account_leagues(profile)
            if str(profile.get("id")) != str(user.team.fpl_team_id):
                raise ValueError("FPL profile does not match the linked team")
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        sources = ["fpl", "legacy"] if options["retire_legacy"] else ["fpl"]
        stale = (
            user.customminileagues.filter(active=True, source__in=sources)
            .exclude(mini_league__fpl_league_id__in=current)
            .select_related("mini_league")
        )
        self.stdout.write(f"Current FPL custom leagues: {len(current)}")
        for row in stale:
            self.stdout.write(
                f"Deactivate: {row.mini_league.fpl_league_id} | {row.mini_league.name} | {row.source}"
            )
        if options["apply"]:
            reconcile_memberships(user, profile, retire_legacy=options["retire_legacy"])
            self.stdout.write(
                self.style.SUCCESS("Memberships reconciled; league history retained.")
            )
        else:
            self.stdout.write("Dry run. Use --apply to reconcile these memberships.")

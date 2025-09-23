from django.core.management.base import BaseCommand

from rivals.services.player_sync_service import PlayerSyncService
from rivals.services.transfer_player_association_service import (
    TransferPlayerAssociationService,
)


class Command(BaseCommand):
    help = "Sync all players from FPL API and update transfer associations"

    def handle(self, *args, **options):
        self.stdout.write("Starting player sync...")

        service = PlayerSyncService()
        result = service.sync_all_players()

        if result["success"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ Successfully synced {result['updated']} players"
                )
            )
            self.stdout.write(f"📊 New players: {result['new_count']}")
            self.stdout.write(f"🔄 Updated players: {result['updated_count']}")

            if result["errors"]:
                self.stdout.write(self.style.WARNING("⚠️ Some errors occurred:"))
                for error in result["errors"]:
                    self.stdout.write(f"  - {error}")
        else:
            self.stdout.write(self.style.ERROR(f"❌ Sync failed: {result['error']}"))
            return "1"

        self.stdout.write("\nUpdating transfer associations...")

        transfer_service = TransferPlayerAssociationService()
        transfer_result = transfer_service.associate_all_transfers()

        self.stdout.write(
            self.style.SUCCESS(
                f"✅ Associated {transfer_result['associated_count']} transfer records"
            )
        )
        if transfer_result["unmatched_count"] > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"⚠️ {transfer_result['unmatched_count']} transfers couldn't be matched"
                )
            )

        return "0"

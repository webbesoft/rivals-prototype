import json

from django.core.management.base import BaseCommand, CommandError

from rivals.models import DecisionRecord
from rivals.services.decision_review_service import calibration, review_decisions


class Command(BaseCommand):
    help = "Review saved forecasts against their unchanged-squad baseline using final scores"

    def handle(self, *args, **options):
        try:
            reviewed = review_decisions()
        except Exception as error:
            raise CommandError("Could not load final gameweek scores.") from error
        result = {
            "updated_plans": reviewed,
            **calibration(DecisionRecord.objects.all()),
        }
        self.stdout.write(json.dumps(result))

from django.db import models


class RequestLimit(models.Model):
    """Shared fixed-window counters; subjects are keyed hashes, never addresses."""

    key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)

from django.conf import settings
from django.db import models


class Connection(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    application = models.ForeignKey(
        "oauth2_provider.Application", on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    authorized_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "application"], name="mcp_user_client"
            )
        ]

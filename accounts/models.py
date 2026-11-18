import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower

from rivals.models import MiniLeague, Team

from .lifecycle import onboarding_deadline
from .timezones import validate_timezone


# Create your models here.
class User(AbstractUser):
    fpl_id = models.CharField(max_length=30, null=True)
    fpl_revision = models.PositiveBigIntegerField(default=0)
    email = models.EmailField(unique=True)
    manager_name = models.CharField(max_length=100, null=True)
    team_name = models.CharField(max_length=30, null=True)
    timezone = models.CharField(
        max_length=64, blank=True, default="", validators=[validate_timezone]
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )
    active = models.BooleanField(default=False)
    password = models.CharField(max_length=255)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Cleared permanently after successful setup; sign-ins never extend it.
    onboarding_expires_at = models.DateTimeField(
        default=onboarding_deadline, null=True, blank=True, db_index=True
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username", "password"]

    mini_leagues = models.ManyToManyField(
        MiniLeague, through="rivals.UserMiniLeague", related_name="users"
    )

    @property
    def can_add_mini_league(self) -> bool:
        """
        Checks if the user can add another mini-league (limit of 10).
        """
        MINI_LEAGUE_LIMIT = 10
        return (
            self.customminileagues.filter(active=True, mini_league__active=True).count()
            < MINI_LEAGUE_LIMIT
        )

    @property
    def can_track_team(self) -> bool:
        """
        Checks if the user can track another team (limit of 10).
        """
        TRACKED_TEAM_LIMIT = 10
        return self.tracked_teams.filter(active=True).count() < TRACKED_TEAM_LIMIT

    def admin_leagues(self):
        """
        Returns a QuerySet of mini-leagues administered by this user.
        """
        if not self.team or not self.team.fpl_team_id:
            return MiniLeague.objects.none()

        return self.mini_leagues.filter(admin_entry_id=self.team.fpl_team_id)

    def __str__(self):
        return self.email

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(
                Lower("email"), name="accounts_user_email_ci_unique"
            ),
        ]


class SignInLink(models.Model):
    # The raw bearer secret is delivered by email and never persisted.
    digest = models.CharField(max_length=64, unique=True)
    email = models.EmailField()
    redirect_to = models.CharField(max_length=2048, default="")
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class PasskeyChallenge(models.Model):
    class Purpose(models.TextChoices):
        REGISTER = "register"
        LOGIN = "login"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_digest = models.CharField(max_length=64)
    user = models.ForeignKey(User, null=True, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=8, choices=Purpose.choices)
    state = models.JSONField()
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True)

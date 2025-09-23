from django.contrib.auth.models import AbstractUser
from django.db import models

from rivals.models import MiniLeague, Team


# Create your models here.
class User(AbstractUser):
    fpl_id = models.CharField(max_length=30, null=True)
    email = models.EmailField(unique=True)
    manager_name = models.CharField(max_length=100, null=True)
    team_name = models.CharField(max_length=30, null=True)
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
        return self.mini_leagues.count() < MINI_LEAGUE_LIMIT

    @property
    def can_track_team(self) -> bool:
        """
        Checks if the user can track another team (limit of 10).
        """
        TRACKED_TEAM_LIMIT = 10
        return self.tracked_teams.count() < TRACKED_TEAM_LIMIT

    def admin_leagues(self):
        """
        Returns a QuerySet of mini-leagues administered by this user.
        """
        if not self.team or not self.team.fpl_team_id:
            return MiniLeague.objects.none()

        return self.mini_leagues.filter(admin_entry_id=self.team.fpl_team_id)

    def __str__(self):
        return self.email

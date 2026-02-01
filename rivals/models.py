from decimal import Decimal

from django.conf import settings
from django.db import models


# Create your models here.
class Team(models.Model):
    fpl_team_id = models.CharField(max_length=255, unique=True)
    team_name = models.CharField(max_length=255)
    manager_name = models.CharField(max_length=255)
    current_total_points = models.IntegerField(default=0)
    current_overall_rank = models.IntegerField(null=True)
    active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True)
    bank = models.IntegerField(default=0)
    value = models.IntegerField(default=1000)
    total_transfers = models.IntegerField(default=0)
    fpl_favourite_team_id = models.IntegerField(null=True)
    years_active = models.IntegerField(null=True)
    started_event = models.IntegerField(null=True)
    current_event_points = models.IntegerField(default=0)
    current_event_rank = models.IntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def rank_change_in_league(self, league_id):
        """Get rank change for this team in a specific mini league"""
        try:
            team_data = self.mini_league_teams.get(mini_league_id=league_id)
            if not team_data.previous_rank:
                return 0
            return team_data.previous_rank - team_data.current_rank
        except self.mini_league_teams.model.DoesNotExist:
            return 0

    @property
    def bank_in_millions(self):
        """Get bank value in millions"""
        return self.bank / 10.0 if self.bank else 0.0

    @property
    def value_in_millions(self):
        """Get total value in millions"""
        return self.value / 10.0 if self.value else 0.0

    @property
    def available_funds(self):
        """Get available funds (alias for bank_in_millions)"""
        return self.bank_in_millions

    @property
    def squad_value(self):
        """Get squad value (total value minus bank)"""
        if self.value and self.bank:
            return self.value_in_millions - self.bank_in_millions
        return None

    def get_squad_for_gameweek(self, gameweek):
        """Get squad for a specific gameweek"""
        return self.squad_histories.filter(gameweek=gameweek).order_by("position")

    def get_available_gameweeks(self):
        """Get list of available gameweeks"""
        available_gameweeks = (
            self.squad_histories.values_list("gameweek", flat=True)
            .distinct()
            .order_by("gameweek")
        )
        return list(available_gameweeks)

    class Meta:
        indexes = [
            models.Index(fields=["fpl_team_id"]),
        ]

    def __str__(self):
        return self.team_name


class Player(models.Model):
    fpl_id = models.IntegerField(unique=True)
    web_name = models.CharField(max_length=255)
    first_name = models.CharField(max_length=255, null=True, blank=True)
    second_name = models.CharField(max_length=255, null=True, blank=True)
    element_type = models.IntegerField()
    team_id = models.IntegerField(null=True, blank=True)
    now_cost = models.IntegerField(null=True, blank=True)
    total_points = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=255, null=True, blank=True)
    selected_by_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    form = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    photo = models.CharField(max_length=255, null=True, blank=True)
    can_transact = models.BooleanField(default=True)
    removed = models.BooleanField(default=False)
    news = models.TextField(null=True, blank=True)
    news_added = models.DateTimeField(null=True, blank=True)
    minutes = models.IntegerField(default=0)
    goals_scored = models.IntegerField(default=0)
    assists = models.IntegerField(default=0)
    clean_sheets = models.IntegerField(default=0)
    goals_conceded = models.IntegerField(default=0)
    yellow_cards = models.IntegerField(default=0)
    red_cards = models.IntegerField(default=0)
    bonus = models.IntegerField(default=0)
    expected_goals = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.0")
    )
    expected_assists = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.0")
    )
    ict_index = models.DecimalField(
        max_digits=4, decimal_places=1, default=Decimal("0.0")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["fpl_id"]),
            models.Index(fields=["element_type"]),
            models.Index(fields=["element_type", "total_points"]),
            models.Index(fields=["team_id"]),
            models.Index(fields=["web_name"]),
        ]

    def __str__(self):
        return self.web_name


class MiniLeague(models.Model):
    fpl_league_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    league_type = models.CharField(max_length=255, null=True, blank=True)
    scoring_type = models.CharField(max_length=255, null=True, blank=True)
    has_cup = models.BooleanField(default=False)
    admin_entry_id = models.CharField(max_length=255, null=True, blank=True)
    start_event = models.IntegerField(null=True, blank=True)
    entry_can_leave = models.BooleanField(default=False)
    entry_can_admin = models.BooleanField(default=False)
    entry_can_invite = models.BooleanField(default=False)
    rank_count = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["fpl_league_id"]),
            models.Index(fields=["admin_entry_id"]),
            models.Index(fields=["league_type"]),
        ]

    def is_system_league(self):
        return self.league_type == "s"

    def is_custom_league(self):
        return self.league_type == "x"

    def is_classic_scoring(self):
        return self.scoring_type == "c"

    def is_head_to_head_scoring(self):
        return self.scoring_type == "h"

    def get_team_standing(self, team):
        """Get a specific team's standing in this mini league"""
        return self.mini_league_teams.filter(team=team).first()

    def get_user_standing(self, user):
        """Get a user's team standing in this mini league"""
        if not user.team:
            return None
        return self.get_team_standing(user.team)

    teams = models.ManyToManyField(
        Team,
        through="MiniLeagueTeam",
        related_name="mini_leagues",
    )

    def __str__(self):
        return self.name


class GameweekData(models.Model):
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="gameweek_data"
    )
    gameweek = models.IntegerField()
    points = models.IntegerField(default=0)
    total_points = models.IntegerField(default=0)
    bench_points = models.IntegerField(default=0)
    rank = models.IntegerField(null=True, blank=True)
    rank_sort = models.IntegerField(null=True, blank=True)
    event_transfers = models.IntegerField(default=0)
    event_transfers_cost = models.IntegerField(default=0)
    captain_name = models.CharField(max_length=255, null=True, blank=True)
    vice_captain_name = models.CharField(max_length=255, null=True, blank=True)
    chip_played = models.BooleanField(default=False)
    chip_name = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        models.UniqueConstraint(
            fields=["team", "gameweek"], name="unique_team_gameweek"
        )
        indexes = [
            models.Index(fields=["team"]),
            models.Index(fields=["gameweek"]),
        ]

    def __str__(self):
        return f"Gameweek {self.gameweek} Data for {self.team.team_name}"


class MiniLeagueTeam(models.Model):
    mini_league = models.ForeignKey(
        MiniLeague, on_delete=models.CASCADE, related_name="mini_league_teams"
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="mini_league_teams"
    )
    current_rank = models.IntegerField()
    previous_rank = models.IntegerField(null=True, blank=True)
    total_points = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        models.UniqueConstraint(
            fields=["mini_league", "team"], name="unique_mini_league_team"
        )
        indexes = [
            models.Index(fields=["mini_league"]),
            models.Index(fields=["team"]),
        ]

    @property
    def rank_change(self):
        if self.previous_rank is None or self.current_rank is None:
            return 0
        return self.previous_rank - self.current_rank

    @property
    def rank_change_direction(self):
        change = self.rank_change
        if change == 0:
            return "no_change"
        return "up" if change > 0 else "down"

    def __str__(self):
        return f"{self.team.team_name} in {self.mini_league.name}"


class Transfer(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="transfers")
    gameweek = models.IntegerField()
    player_in_name = models.CharField(max_length=255)
    player_out_name = models.CharField(max_length=255)
    player_in_cost = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True
    )
    player_out_cost = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True
    )
    transfer_time = models.DateTimeField(null=True, blank=True)
    player_in = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfers_in",
    )
    player_out = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfers_out",
    )
    point_cost = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def player_in_display_name(self) -> str:
        """
        Returns the linked player's web_name, falling back to the stored name.
        """
        if self.player_in:
            return self.player_in.web_name
        return self.player_in_name or "Unknown Player"

    @property
    def player_out_display_name(self) -> str:
        """
        Returns the linked player's web_name, falling back to the stored name.
        """
        if self.player_out:
            return self.player_out.web_name
        return self.player_out_name or "Unknown Player"

    @property
    def point_cost_display(self) -> str:
        """
        Formats the point cost for display (e.g., 'Free' or '-4pt').
        """
        return "Free" if self.point_cost == 0 else f"-{self.point_cost}pt"

    @property
    def price_difference(self) -> Decimal | None:
        """
        Calculates the price difference between the players in millions.
        """
        if self.player_in_cost is None or self.player_out_cost is None:
            return None
        # Note: Costs are stored as price * 10, so we divide to get millions
        return self.player_in_cost - self.player_out_cost

    @property
    def price_difference_display(self) -> str:
        """
        Formats the price difference for display (e.g., '+£1.5M').
        """
        diff = self.price_difference
        if diff is None:
            return "N/A"

        sign = "+" if diff > 0 else ""
        return f"{sign}£{abs(diff)}M"

    @property
    def transfer_summary(self) -> str:
        """
        Creates a concise summary string for the transfer.
        Example: 'Son → Salah (+£1.5M, -4pt)'
        """
        summary = f"{self.player_out_display_name} → {self.player_in_display_name}"

        parts = []
        if self.price_difference is not None and self.price_difference != 0:
            parts.append(self.price_difference_display)
        if self.point_cost > 0:
            parts.append(self.point_cost_display)

        if parts:
            summary += f" ({', '.join(parts)})"

        return summary

    class Meta:
        indexes = [
            models.Index(fields=["team"]),
            models.Index(fields=["team", "gameweek"]),
            models.Index(fields=["gameweek", "point_cost"]),
            models.Index(fields=["player_in"]),
            models.Index(fields=["player_out"]),
            models.Index(fields=["point_cost"]),
        ]

    def __str__(self):
        return f"GW {self.gameweek}: {self.player_out_name} out, {self.player_in_name} in for {self.team.team_name}"


class UserMiniLeague(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customminileagues",
    )
    mini_league = models.ForeignKey(
        MiniLeague, on_delete=models.CASCADE, related_name="user_minileagues"
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        models.UniqueConstraint(
            fields=["user", "mini_league"], name="unique_user_mini_league"
        )
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["mini_league"]),
        ]

    def __str__(self):
        return f"{self.user.username} in {self.mini_league.name}"

    def get_user_team_standing(self):
        """Get this user's team standing in the mini league"""
        if not self.user.team:
            return None
        return self.mini_league.mini_league_teams.filter(team=self.user.team).first()


class TrackedTeam(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tracked_teams"
    )
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        models.UniqueConstraint(fields=["user", "team"], name="unique_user_team")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["team"]),
        ]

    def __str__(self):
        return f"User {self.user.username} tracking {self.team.team_name}"


class SquadHistory(models.Model):
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="squad_histories"
    )
    gameweek = models.IntegerField()
    player_fpl_id = models.IntegerField()
    position = models.IntegerField()
    multiplier = models.IntegerField()
    is_captain = models.BooleanField(default=False)
    is_vice_captain = models.BooleanField(default=False)
    element_type = models.IntegerField()
    player_name = models.CharField(max_length=255)
    player_cost = models.IntegerField()
    form = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    expected_goals = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    expected_assists = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    selected_by_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    total_points_at_time = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def formatted_cost(self) -> str:
        """Formats the player cost for display (e.g., '£5.5m')."""
        cost_in_millions = self.player_cost / 10.0
        return f"£{cost_in_millions:.1f}m"

    @property
    def position_type(self) -> str:
        """Returns the player's position as a short string (e.g., 'GKP')."""
        position_map = {
            1: "GKP",
            2: "DEF",
            3: "MID",
            4: "FWD",
        }
        return position_map.get(self.element_type, "UNK")

    @property
    def is_starting_eleven(self) -> bool:
        """Returns True if the player is in the starting lineup."""
        return self.position <= 11

    @property
    def is_bench(self) -> bool:
        """Returns True if the player is on the bench."""
        return self.position > 11

    class Meta:
        models.UniqueConstraint(
            fields=["team", "gameweek", "player_fpl_id"],
            name="unique_team_gameweek_player_fpl_id",
        )
        indexes = [
            models.Index(fields=["team", "gameweek"]),
            models.Index(fields=["player_fpl_id"]),
        ]

    def __str__(self):
        return (
            f"{self.player_name} in Gameweek {self.gameweek} for {self.team.team_name}"
        )


class TransferPlan(models.Model):
    """User's saved transfer plan for simulating future transfers."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transfer_plans",
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="transfer_plans"
    )
    name = models.CharField(max_length=100, default="My Plan")
    is_active = models.BooleanField(default=True)  # Current working plan
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "is_active"]),
        ]

    def __str__(self):
        return f"{self.name} - {self.team.team_name}"


class PlannedTransfer(models.Model):
    """Individual transfer within a transfer plan."""

    plan = models.ForeignKey(
        TransferPlan, on_delete=models.CASCADE, related_name="transfers"
    )
    player_out = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_transfers_out",
    )
    player_in = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_transfers_in",
    )
    gameweek = models.IntegerField()  # Target GW for this transfer
    order = models.IntegerField(default=0)  # For multi-transfer sequencing

    class Meta:
        ordering = ["gameweek", "order"]
        indexes = [
            models.Index(fields=["plan", "gameweek"]),
        ]

    def __str__(self):
        out_name = getattr(self.player_out, "web_name", "Unknown")
        in_name = getattr(self.player_in, "web_name", "Unknown")
        return f"GW{self.gameweek}: {out_name} → {in_name}"

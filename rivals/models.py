from decimal import Decimal
import uuid

from django.conf import settings
from django.db import models


class DigestSubscription(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    paused = models.BooleanField(default=False)
    lead_hours = models.PositiveSmallIntegerField(default=4)
    consented_at = models.DateTimeField(null=True)
    # Rotating this token when re-enabling prevents an old email disabling a new consent.
    unsubscribe_token = models.UUIDField(default=uuid.uuid4, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class DeadlineDigest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    team = models.ForeignKey("Team", on_delete=models.CASCADE)
    season = models.CharField(max_length=7)
    gameweek = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=12, default="scheduled")
    deadline = models.DateTimeField()
    scheduled_for = models.DateTimeField()
    generation_state = models.CharField(max_length=16, default="pending")
    delivery_state = models.CharField(max_length=16, default="pending")
    generation = models.PositiveIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    delivery_attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    prepared_at = models.DateTimeField(null=True)
    sent_at = models.DateTimeField(null=True)
    recipient = models.EmailField(blank=True)
    report = models.JSONField(default=dict)
    ai_briefing = models.ForeignKey("AIBriefing", null=True, on_delete=models.SET_NULL)
    notice = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def status_label(self):
        from django.utils import timezone

        if self.delivery_state == "sent":
            return "Sent"
        if self.delivery_state == "ambiguous":
            return "Delivery unconfirmed"
        if self.delivery_state == "skipped":
            return "Skipped"
        if self.delivery_state in ("connecting", "sending"):
            return "Sending"
        if self.generation_state == "ready":
            if self.kind == "preview":
                return "Preview ready"
            return "Could not send" if self.delivery_attempts >= 3 else "Ready to send"
        if self.generation_state == "failed":
            return "Could not prepare" if self.attempts >= 3 else "Waiting to retry"
        if self.scheduled_for > timezone.now():
            return "Scheduled"
        if self.attempts >= 3 and self.generation_state == "pending":
            return "Could not prepare"
        return "Preparing"

    @property
    def is_processing(self):
        return self.status_label in (
            "Preparing",
            "Sending",
            "Ready to send",
            "Waiting to retry",
        )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "season", "gameweek"],
                condition=models.Q(kind="scheduled"),
                name="one_deadline_digest_per_event",
            )
        ]
        indexes = [models.Index(fields=["generation_state", "scheduled_for"])]


class AICredential(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    ciphertext = models.TextField(editable=False)
    key_version = models.CharField(max_length=32, editable=False)
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
    verified_at = models.DateTimeField()
    consented_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)


class AIUsage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField()
    connections = models.PositiveSmallIntegerField(default=0)
    generations = models.PositiveSmallIntegerField(default=0)
    reserved_tokens = models.PositiveIntegerField(default=0)
    last_connection_at = models.DateTimeField(null=True)
    last_generation_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "date"], name="ai_daily_usage")
        ]


class AIBriefing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    team = models.ForeignKey("Team", on_delete=models.CASCADE)
    credential_revision = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default="queued")
    evidence = models.JSONField(default=list)
    priorities = models.JSONField(default=list)
    model = models.CharField(max_length=64)
    error_code = models.CharField(max_length=24, blank=True)
    input_tokens = models.PositiveIntegerField(null=True)
    output_tokens = models.PositiveIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True)
    trigger = models.CharField(max_length=16, default="planner")
    source_digest_id = models.UUIDField(null=True)


class AIProviderCall(models.Model):
    """One paid-call claim per briefing, retained when results are revoked."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    briefing = models.OneToOneField(AIBriefing, null=True, on_delete=models.SET_NULL)
    briefing_reference = models.UUIDField(unique=True)
    credential_revision = models.UUIDField()
    model = models.CharField(max_length=64)
    trigger = models.CharField(max_length=16)
    source_digest_id = models.UUIDField(null=True)
    reserved_tokens = models.PositiveIntegerField()
    outcome = models.CharField(max_length=16, default="prepared")
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    http_status = models.PositiveSmallIntegerField(null=True)
    duration_ms = models.PositiveIntegerField(null=True)
    input_bytes = models.PositiveIntegerField(null=True)
    provider_response_id = models.CharField(max_length=128, blank=True)
    input_tokens = models.PositiveBigIntegerField(null=True)
    candidate_tokens = models.PositiveBigIntegerField(null=True)
    thought_tokens = models.PositiveBigIntegerField(null=True)
    cached_tokens = models.PositiveBigIntegerField(null=True)
    total_tokens = models.PositiveBigIntegerField(null=True)
    usage_complete = models.BooleanField(default=False)
    usage_exceeds_reservation = models.BooleanField(default=False)
    reviewed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AIUsageEvent(models.Model):
    """Append-only application audit metadata; no prompts, keys or response text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    briefing_id = models.UUIDField(null=True)
    call_id = models.UUIDField(null=True)
    source_digest_id = models.UUIDField(null=True)
    trigger = models.CharField(max_length=16, blank=True)
    event = models.CharField(max_length=32)
    reason = models.CharField(max_length=32, blank=True)
    reserved_tokens = models.PositiveIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "created_at"])]


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
    # Season tracking — set to the FPL season string e.g. "2025/26"
    current_season = models.CharField(max_length=20, null=True, blank=True)
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

    def get_squad_for_gameweek(self, gameweek, season=None):
        """Get squad for a specific gameweek"""
        return self.squad_histories.filter(
            gameweek=gameweek, season=season or self.current_season
        ).order_by("position")

    def get_available_gameweeks(self, season=None):
        """Get list of available gameweeks"""
        available_gameweeks = (
            self.squad_histories.filter(season=season or self.current_season)
            .values_list("gameweek", flat=True)
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
    summary_only = models.BooleanField(default=False)
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

    @property
    def uses_summary(self):
        return (
            self.summary_only
            or (self.rank_count or 0) > settings.LEAGUE_FULL_IMPORT_LIMIT
        )

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
    season = models.CharField(max_length=20, null=True, blank=True)
    points = models.IntegerField(default=0)
    total_points = models.IntegerField(default=0)
    bench_points = models.IntegerField(default=0)
    rank = models.IntegerField(null=True, blank=True)
    rank_sort = models.IntegerField(null=True, blank=True)
    bank = models.IntegerField(null=True, blank=True)
    event_transfers = models.IntegerField(default=0)
    event_transfers_cost = models.IntegerField(default=0)
    captain_name = models.CharField(max_length=255, null=True, blank=True)
    vice_captain_name = models.CharField(max_length=255, null=True, blank=True)
    chip_played = models.BooleanField(default=False)
    chip_name = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "gameweek", "season"],
                name="unique_team_gameweek_season",
            )
        ]
        indexes = [
            models.Index(fields=["team"]),
            models.Index(fields=["gameweek"]),
            models.Index(fields=["team", "season"]),
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
    season = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mini_league", "team"], name="unique_mini_league_team"
            )
        ]
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
    season = models.CharField(max_length=20, null=True, blank=True)
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
        # FPL costs are in tenths of a million (e.g. 75 means £7.5m).
        return (self.player_in_cost - self.player_out_cost) / Decimal("10")

    @property
    def price_difference_display(self) -> str:
        """
        Formats the price difference for display (e.g., '+£1.5M').
        """
        diff = self.price_difference
        if diff is None:
            return "N/A"

        sign = "+" if diff > 0 else "-" if diff < 0 else ""
        return f"{sign}£{abs(diff):.1f}M"

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
    source = models.CharField(
        max_length=10,
        choices=[("legacy", "Legacy"), ("fpl", "FPL account"), ("manual", "Manual")],
        default="legacy",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "mini_league"], name="unique_user_mini_league"
            )
        ]
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
        constraints = [
            models.UniqueConstraint(fields=["user", "team"], name="unique_user_team")
        ]
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
    season = models.CharField(max_length=20, null=True, blank=True)
    player_fpl_id = models.IntegerField()
    position = models.IntegerField()
    multiplier = models.IntegerField()
    is_captain = models.BooleanField(default=False)
    is_vice_captain = models.BooleanField(default=False)
    element_type = models.IntegerField()
    player_name = models.CharField(max_length=255)
    club_name = models.CharField(max_length=100, blank=True, default="")
    club_short_name = models.CharField(max_length=10, blank=True, default="")
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
    gameweek_points = models.IntegerField(null=True, blank=True)
    points_final = models.BooleanField(default=False)
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
        constraints = [
            models.UniqueConstraint(
                fields=["team", "gameweek", "player_fpl_id", "season"],
                name="unique_team_gameweek_player_fpl_id",
            )
        ]
        indexes = [
            models.Index(fields=["team", "gameweek"]),
            models.Index(fields=["player_fpl_id"]),
        ]

    def __str__(self):
        return (
            f"{self.player_name} in Gameweek {self.gameweek} for {self.team.team_name}"
        )


class SeasonHistory(models.Model):
    """Per-season summary for a team, sourced from FPL history.past endpoint."""

    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="season_histories"
    )
    season_name = models.CharField(max_length=20)  # e.g. "2024/25"
    total_points = models.IntegerField(default=0)
    overall_rank = models.IntegerField(null=True, blank=True)
    rank = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "season_name"], name="unique_team_season"
            )
        ]
        ordering = ["-season_name"]
        indexes = [
            models.Index(fields=["team", "season_name"]),
        ]

    def __str__(self):
        return f"{self.team.team_name} — {self.season_name}"


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
    season = models.CharField(max_length=20, null=True, blank=True)
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


class SyncState(models.Model):
    """Durable refresh status shared by web and task workers."""

    key = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, default="pending")
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    generation = models.PositiveIntegerField(default=0)
    failures = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict)


class TeamPlanningInput(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    season = models.CharField(max_length=20)
    gameweek = models.PositiveSmallIntegerField()
    free_transfers = models.PositiveSmallIntegerField(null=True, blank=True)
    free_transfers_recorded_at = models.DateTimeField(null=True, blank=True)
    bank_recorded_at = models.DateTimeField(null=True, blank=True)
    selling_price_markets = models.JSONField(default=dict)
    bank = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    selling_prices = models.JSONField(default=dict)
    squad_ids = models.JSONField(default=list)
    squad_confirmed = models.BooleanField(default=False)
    manual_moves = models.JSONField(default=list)
    horizon = models.PositiveSmallIntegerField(default=5)
    strategy = models.CharField(max_length=10, default="protect")
    target_league = models.ForeignKey(
        MiniLeague, on_delete=models.SET_NULL, null=True, blank=True
    )
    target_rival = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="targeted_planning_inputs",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "team", "season", "gameweek"],
                name="unique_manager_planning_input",
            )
        ]


class LeagueImport(models.Model):
    """Unpublished standings, checkpointed independently of refresh attempts."""

    league = models.OneToOneField(MiniLeague, on_delete=models.CASCADE)
    stamp = models.CharField(max_length=40)
    next_page = models.PositiveIntegerField(default=1)
    league_info = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class LeagueImportPage(models.Model):
    import_run = models.ForeignKey(
        LeagueImport, on_delete=models.CASCADE, related_name="pages"
    )
    number = models.PositiveIntegerField()
    standings = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["import_run", "number"], name="unique_league_import_page"
            )
        ]


class DecisionRecord(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    season = models.CharField(max_length=20)
    gameweek = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=10,
        default="planned",
        choices=[
            ("planned", "Saved plan"),
            ("acted", "Followed plan"),
            ("held", "Kept squad"),
        ],
    )
    fingerprint = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict)
    outcomes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "fingerprint"], name="unique_saved_decision"
            )
        ]


class LeagueRankSnapshot(models.Model):
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="league_ranks"
    )
    mini_league = models.ForeignKey(
        MiniLeague, on_delete=models.CASCADE, related_name="rank_snapshots"
    )
    season = models.CharField(max_length=20)
    gameweek = models.PositiveSmallIntegerField()
    current_rank = models.PositiveIntegerField()
    previous_rank = models.PositiveIntegerField(null=True, blank=True)
    total_points = models.IntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "mini_league", "season", "gameweek"],
                name="unique_league_rank_snapshot",
            )
        ]

    @property
    def rank_change(self):
        return self.previous_rank - self.current_rank if self.previous_rank else None


class LeagueStandingsPage(models.Model):
    mini_league = models.ForeignKey(
        MiniLeague, on_delete=models.CASCADE, related_name="standings_pages"
    )
    season = models.CharField(max_length=20)
    number = models.PositiveIntegerField()
    rows = models.JSONField(default=list)
    has_next = models.BooleanField(default=False)
    page_size = models.PositiveIntegerField(default=50)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mini_league", "season", "number"],
                name="unique_league_standings_page",
            )
        ]

from django.contrib.auth import get_user_model
from rest_framework import serializers

from rivals.models import (
    GameweekData,
    MiniLeague,
    MiniLeagueTeam,
    Player,
    SquadHistory,
    Team,
    Transfer,
    TransferPlan,
    PlannedTransfer,
)

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "manager_name",
            "team_name",
            "fpl_id",
            "is_verified",
            "active",
            "created_at",
        ]
        read_only_fields = fields


class SignUpSerializer(serializers.Serializer):
    fpl_id = serializers.IntegerField(required=True)
    email = serializers.EmailField(required=True)
    password1 = serializers.CharField(write_only=True, required=True, min_length=8)
    password2 = serializers.CharField(write_only=True, required=True, min_length=8)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate(self, data):
        if data["password1"] != data["password2"]:
            raise serializers.ValidationError({"password2": "Passwords do not match."})
        return data


class TeamSerializer(serializers.ModelSerializer):
    bank_in_millions = serializers.FloatField(read_only=True)
    value_in_millions = serializers.FloatField(read_only=True)
    squad_value = serializers.FloatField(read_only=True)

    class Meta:
        model = Team
        fields = [
            "id",
            "fpl_team_id",
            "team_name",
            "manager_name",
            "current_total_points",
            "current_overall_rank",
            "current_event_points",
            "current_event_rank",
            "bank_in_millions",
            "value_in_millions",
            "squad_value",
            "total_transfers",
            "years_active",
            "last_synced_at",
        ]


class MiniLeagueTeamSerializer(serializers.ModelSerializer):
    team = TeamSerializer(read_only=True)
    rank_change = serializers.IntegerField(read_only=True)
    rank_change_direction = serializers.CharField(read_only=True)

    class Meta:
        model = MiniLeagueTeam
        fields = [
            "id",
            "current_rank",
            "previous_rank",
            "total_points",
            "rank_change",
            "rank_change_direction",
            "team",
        ]


class MiniLeagueSerializer(serializers.ModelSerializer):
    class Meta:
        model = MiniLeague
        fields = [
            "id",
            "fpl_league_id",
            "name",
            "description",
            "league_type",
            "scoring_type",
            "has_cup",
            "rank_count",
            "last_synced_at",
        ]


class MiniLeagueDetailSerializer(MiniLeagueSerializer):
    standings = MiniLeagueTeamSerializer(
        source="mini_league_teams", many=True, read_only=True
    )

    class Meta(MiniLeagueSerializer.Meta):
        fields = MiniLeagueSerializer.Meta.fields + ["standings"]


class GameweekDataSerializer(serializers.ModelSerializer):
    class Meta:
        model = GameweekData
        fields = [
            "id",
            "gameweek",
            "points",
            "total_points",
            "bench_points",
            "rank",
            "event_transfers",
            "event_transfers_cost",
            "captain_name",
            "vice_captain_name",
            "chip_played",
            "chip_name",
        ]


class PlayerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Player
        fields = [
            "id",
            "fpl_id",
            "web_name",
            "first_name",
            "second_name",
            "element_type",
            "team_id",
            "now_cost",
            "total_points",
            "status",
            "selected_by_percent",
            "form",
            "photo",
        ]


class TransferSerializer(serializers.ModelSerializer):
    player_in_display_name = serializers.CharField(read_only=True)
    player_out_display_name = serializers.CharField(read_only=True)
    transfer_summary = serializers.CharField(read_only=True)
    price_difference_display = serializers.CharField(read_only=True)

    class Meta:
        model = Transfer
        fields = [
            "id",
            "gameweek",
            "player_in_display_name",
            "player_out_display_name",
            "player_in_cost",
            "player_out_cost",
            "point_cost",
            "transfer_time",
            "transfer_summary",
            "price_difference_display",
        ]


class SquadHistorySerializer(serializers.ModelSerializer):
    formatted_cost = serializers.CharField(read_only=True)
    position_type = serializers.CharField(read_only=True)
    is_starting_eleven = serializers.BooleanField(read_only=True)
    is_bench = serializers.BooleanField(read_only=True)

    class Meta:
        model = SquadHistory
        fields = [
            "id",
            "gameweek",
            "player_fpl_id",
            "position",
            "multiplier",
            "is_captain",
            "is_vice_captain",
            "element_type",
            "player_name",
            "player_cost",
            "formatted_cost",
            "position_type",
            "is_starting_eleven",
            "is_bench",
            "form",
            "expected_goals",
            "expected_assists",
            "selected_by_percent",
            "total_points_at_time",
        ]


class PlannedTransferSerializer(serializers.ModelSerializer):
    player_out_name = serializers.CharField(
        source="player_out.web_name", read_only=True
    )
    player_in_name = serializers.CharField(source="player_in.web_name", read_only=True)
    player_out_team = serializers.CharField(source="player_out.team", read_only=True)
    player_in_team = serializers.CharField(source="player_in.team", read_only=True)
    player_in_cost = serializers.FloatField(source="player_in.now_cost", read_only=True)
    player_out_cost = serializers.FloatField(
        source="player_out.now_cost", read_only=True
    )

    class Meta:
        model = PlannedTransfer
        fields = [
            "id",
            "plan",
            "player_out",
            "player_in",
            "gameweek",
            "order",
            "player_out_name",
            "player_in_name",
            "player_out_team",
            "player_in_team",
            "player_in_cost",
            "player_out_cost",
        ]
        read_only_fields = ["plan"]


class TransferPlanSerializer(serializers.ModelSerializer):
    transfers = PlannedTransferSerializer(many=True, read_only=True)

    class Meta:
        model = TransferPlan
        fields = [
            "id",
            "user",
            "team",
            "name",
            "is_active",
            "created_at",
            "updated_at",
            "transfers",
        ]
        read_only_fields = ["user", "created_at", "updated_at"]

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data["user"] = user
        return super().create(validated_data)

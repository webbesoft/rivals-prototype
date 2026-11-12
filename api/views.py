import logging

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.db.models import Q
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from rivals.models import MiniLeague, Team
from rivals.services.access_service import (
    TrackingLimitReached,
    accessible_leagues,
    track_team,
)

from .pagination import paginated_response
from .permissions import IsVerified
from .serializers import (
    GameweekDataSerializer,
    MiniLeagueDetailSerializer,
    MiniLeagueSerializer,
    SquadHistorySerializer,
    TeamSerializer,
    TransferSerializer,
    UserSerializer,
)

User = get_user_model()

logger = logging.getLogger(__name__)


class LoginView(APIView):
    """
    Login endpoint that returns an auth token.
    """

    rate_limit_action = "login"
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")

        if not email or not password:
            return Response(
                {"error": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=email, password=password)

        if user is None:
            return Response(
                {"error": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_verified:
            return Response(
                {"error": "Verify your email before logging in."},
                status=status.HTTP_403_FORBIDDEN,
            )

        token, _ = Token.objects.get_or_create(user=user)

        return Response(
            {
                "token": token.key,
                "user": UserSerializer(user).data,
            }
        )


class SignUpView(APIView):
    """
    Signup endpoint that creates a new user account.
    """

    rate_limit_action = "signup"
    permission_classes = [AllowAny]

    def post(self, request):
        from accounts.forms import SignUpForm

        from .serializers import SignUpSerializer

        serializer = SignUpSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"success": False, "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Use the existing SignUpForm to handle user creation
        # (it includes FPL validation and initialization logic)
        form = SignUpForm(
            data={
                "fpl_id": serializer.validated_data["fpl_id"],
                "email": serializer.validated_data["email"],
                "password1": serializer.validated_data["password1"],
                "password2": serializer.validated_data["password2"],
            }
        )

        if form.is_valid():
            try:
                user = form.save()
                return Response(
                    {
                        "success": True,
                        "message": "Account created! Please check your email to verify your account.",
                        "user": UserSerializer(user).data,
                    },
                    status=status.HTTP_201_CREATED,
                )
            except Exception as e:
                return Response(
                    {"success": False, "error": str(e)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            # Convert form errors to API format
            errors = {}
            for field, error_list in form.errors.items():
                errors[field] = [str(e) for e in error_list]
            return Response(
                {"success": False, "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )


class MeView(APIView):
    """
    Returns the current authenticated user's profile.
    """

    permission_classes = [IsVerified]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class DashboardView(APIView):
    """
    Returns dashboard data for the authenticated user.
    """

    permission_classes = [IsVerified]

    def get(self, request):
        user = request.user
        team = user.team

        data = {
            "user": UserSerializer(user).data,
            "team": TeamSerializer(team).data if team else None,
            "leagues_count": user.mini_leagues.count(),
            "tracked_teams_count": user.tracked_teams.filter(active=True).count(),
        }

        # Calculate best league position
        if team:
            try:
                # Get all mini league standings for this team, exclude system leagues if desired
                # For now getting all, ordered by current_rank
                best_standing = (
                    team.mini_league_teams.select_related("mini_league")
                    .exclude(
                        Q(mini_league__summary_only=True)
                        | Q(
                            mini_league__rank_count__gt=settings.LEAGUE_FULL_IMPORT_LIMIT
                        )
                    )
                    .order_by("current_rank")
                    .first()
                )

                from rivals.services.gameweek_context_service import (
                    GameweekContextService,
                )

                published = (
                    team.league_ranks.filter(
                        season=GameweekContextService.cached()["season"],
                        mini_league__in=accessible_leagues(user),
                    )
                    .select_related("mini_league")
                    .order_by("-gameweek", "current_rank")
                    .first()
                )
                if published and (
                    best_standing is None
                    or published.current_rank < best_standing.current_rank
                ):
                    best_standing = published

                if best_standing:
                    data["best_league_rank"] = best_standing.current_rank
                    data["best_league_name"] = best_standing.mini_league.name
            except Exception:
                logger.exception(
                    "Failed to compute best league position for user %s",
                    getattr(request.user, "id", None),
                )
        return Response(data)


class LeaguesView(APIView):
    """
    Returns the list of mini-leagues for the authenticated user.
    """

    permission_classes = [IsVerified]

    def get(self, request):
        leagues = accessible_leagues(request.user)
        return paginated_response(request, leagues.order_by("pk"), MiniLeagueSerializer)


class LeagueDetailView(APIView):
    """
    Returns details and standings for a specific mini-league.
    """

    permission_classes = [IsVerified]

    def get(self, request, league_id):
        try:
            league = MiniLeague.objects.get(id=league_id)
        except MiniLeague.DoesNotExist:
            return Response(
                {"error": "League not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if user has access to this league
        if not accessible_leagues(request.user).filter(pk=league_id).exists():
            return Response(
                {"error": "You do not have access to this league."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if league.uses_summary:
            from rivals.services.gameweek_context_service import GameweekContextService
            from rivals.services.league_summary_service import large_league_context

            data = large_league_context(
                request.user,
                league,
                GameweekContextService.cached(),
                request.query_params,
            )
            rank = data.get("rank_snapshot")
            return Response(
                {
                    **MiniLeagueSerializer(league).data,
                    "partial": True,
                    "standings": data.get("visible_rows", []),
                    "leaders": data.get("leaders", []),
                    "page": data.get("standings_page"),
                    "has_next": data.get("has_next", False),
                    "waiting": data.get("waiting", False),
                    "stale": data.get("pages_stale", True),
                    "user_rank": (
                        {
                            "current_rank": rank.current_rank,
                            "previous_rank": rank.previous_rank,
                            "gameweek": rank.gameweek,
                            "updated_at": rank.updated_at,
                        }
                        if rank
                        else None
                    ),
                }
            )
        serializer = MiniLeagueDetailSerializer(league, context={"request": request})
        return Response(serializer.data)


class LeagueSyncView(APIView):
    """
    Triggers a sync of mini-league data from FPL API.
    """

    rate_limit_action = "refresh"
    permission_classes = [IsVerified]

    def post(self, request, league_id):
        try:
            league = MiniLeague.objects.get(id=league_id)
        except MiniLeague.DoesNotExist:
            return Response(
                {"error": "League not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if user has access to this league
        if not accessible_leagues(request.user).filter(pk=league_id).exists():
            return Response(
                {"error": "You do not have access to this league."},
                status=status.HTTP_403_FORBIDDEN,
            )

        from rivals.services.refresh_service import enqueue_refresh

        enqueue_refresh("league", league.id, force=True)
        if league.uses_summary and request.user.team_id:
            enqueue_refresh("profile", request.user.team_id, force=True)
        return Response(
            {
                "success": True,
                "message": "Standings update requested.",
                "league": MiniLeagueDetailSerializer(
                    league, context={"request": request}
                ).data,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class TeamDetailView(APIView):
    """
    Returns details for a specific team.
    """

    permission_classes = [IsVerified]

    def get(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = TeamSerializer(team)
        return Response(serializer.data)


class TeamGameweekHistoryView(APIView):
    """
    Returns gameweek history for a specific team.
    """

    permission_classes = [IsVerified]

    def get(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        gameweeks = team.gameweek_data.all().order_by("-gameweek")
        return paginated_response(
            request,
            gameweeks.order_by("-season", "-gameweek", "-pk"),
            GameweekDataSerializer,
        )


class TeamTransfersView(APIView):
    """
    Returns transfers for a specific team.
    """

    permission_classes = [IsVerified]

    def get(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        transfers = team.transfers.all().order_by("-gameweek", "-transfer_time")
        return paginated_response(
            request,
            transfers.select_related("player_in", "player_out").order_by(
                "-season", "-gameweek", "-transfer_time", "-pk"
            ),
            TransferSerializer,
        )


class TeamSquadView(APIView):
    """
    Returns squad for a specific team and gameweek.
    """

    permission_classes = [IsVerified]

    def get(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from rivals.services.squad_history_service import SquadHistoryService

        service = SquadHistoryService(team)
        try:
            selected = service.selection(
                request.query_params.get("season"), request.query_params.get("gameweek")
            )
        except ValueError as error:
            return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        squad = team.squad_histories.filter(
            season=selected["season"], gameweek=selected["gameweek"]
        ).order_by("position")
        return Response(
            {
                "season": selected["season"],
                "gameweek": selected["gameweek"],
                "available_gameweeks": selected["available_gameweeks"],
                "squad": SquadHistorySerializer(squad, many=True).data,
            }
        )


class TrackedTeamsView(APIView):
    """
    Returns list of teams tracked by the authenticated user.
    """

    permission_classes = [IsVerified]

    def get(self, request):
        tracked = request.user.tracked_teams.filter(active=True).select_related("team")
        teams = [t.team for t in tracked]
        serializer = TeamSerializer(teams, many=True)
        return Response(serializer.data)


class TeamSyncView(APIView):
    """
    Triggers a sync of team data from FPL API.
    """

    rate_limit_action = "refresh"
    permission_classes = [IsVerified]

    def post(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if (
            request.user.team_id != team.id
            and not request.user.tracked_teams.filter(team=team, active=True).exists()
        ):
            return Response(
                {"error": "Track this team before requesting a refresh."},
                status=status.HTTP_403_FORBIDDEN,
            )
        from rivals.services.refresh_service import enqueue_team

        enqueue_team(team.id, force=True)
        return Response(
            {
                "success": True,
                "message": "Team update requested.",
                "team": TeamSerializer(team).data,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class TeamTrackView(APIView):
    """
    Track or untrack a team for the authenticated user.
    POST to track, DELETE to untrack.
    """

    rate_limit_action = "refresh"
    permission_classes = [IsVerified]

    def post(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            track_team(request.user, team)
        except TrackingLimitReached as error:
            return Response({"error": str(error)}, status=status.HTTP_403_FORBIDDEN)

        from rivals.services.refresh_service import enqueue_team

        enqueue_team(team.id, force=True)

        return Response(
            {
                "success": True,
                "message": "Team is now being tracked.",
                "team": TeamSerializer(team).data,
            }
        )

    def delete(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        request.user.tracked_teams.filter(team=team).delete()

        return Response(
            {
                "success": True,
                "message": "Team untracked successfully.",
            }
        )

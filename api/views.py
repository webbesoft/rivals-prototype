from django.contrib.auth import authenticate, get_user_model
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rivals.models import MiniLeague, Team

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


class LoginView(APIView):
    """
    Login endpoint that returns an auth token.
    """

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

    permission_classes = [AllowAny]

    def post(self, request):
        from .serializers import SignUpSerializer
        from accounts.forms import SignUpForm

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

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class DashboardView(APIView):
    """
    Returns dashboard data for the authenticated user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        team = user.team

        data = {
            "user": UserSerializer(user).data,
            "team": TeamSerializer(team).data if team else None,
            "leagues_count": user.mini_leagues.count(),
            "tracked_teams_count": user.tracked_teams.count(),
        }

        return Response(data)


class LeaguesView(APIView):
    """
    Returns the list of mini-leagues for the authenticated user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        leagues = request.user.mini_leagues.all()
        serializer = MiniLeagueSerializer(leagues, many=True)
        return Response(serializer.data)


class LeagueDetailView(APIView):
    """
    Returns details and standings for a specific mini-league.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, league_id):
        try:
            league = MiniLeague.objects.prefetch_related(
                "mini_league_teams__team"
            ).get(id=league_id)
        except MiniLeague.DoesNotExist:
            return Response(
                {"error": "League not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if user has access to this league
        if not request.user.mini_leagues.filter(id=league_id).exists():
            return Response(
                {"error": "You do not have access to this league."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = MiniLeagueDetailSerializer(league)
        return Response(serializer.data)


class TeamDetailView(APIView):
    """
    Returns details for a specific team.
    """

    permission_classes = [IsAuthenticated]

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

    permission_classes = [IsAuthenticated]

    def get(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        gameweeks = team.gameweek_data.all().order_by("-gameweek")
        serializer = GameweekDataSerializer(gameweeks, many=True)
        return Response(serializer.data)


class TeamTransfersView(APIView):
    """
    Returns transfers for a specific team.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        transfers = team.transfers.all().order_by("-gameweek", "-transfer_time")
        serializer = TransferSerializer(transfers, many=True)
        return Response(serializer.data)


class TeamSquadView(APIView):
    """
    Returns squad for a specific team and gameweek.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        gameweek = request.query_params.get("gameweek")

        if gameweek:
            squad = team.squad_histories.filter(gameweek=int(gameweek)).order_by(
                "position"
            )
        else:
            # Get the latest gameweek
            latest_gw = (
                team.squad_histories.values_list("gameweek", flat=True)
                .distinct()
                .order_by("-gameweek")
                .first()
            )
            if latest_gw:
                squad = team.squad_histories.filter(gameweek=latest_gw).order_by(
                    "position"
                )
            else:
                squad = []

        serializer = SquadHistorySerializer(squad, many=True)
        return Response(
            {
                "gameweek": gameweek or latest_gw,
                "available_gameweeks": team.get_available_gameweeks(),
                "squad": serializer.data,
            }
        )


class TrackedTeamsView(APIView):
    """
    Returns list of teams tracked by the authenticated user.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tracked = request.user.tracked_teams.select_related("team").all()
        teams = [t.team for t in tracked]
        serializer = TeamSerializer(teams, many=True)
        return Response(serializer.data)


class TeamSyncView(APIView):
    """
    Triggers a sync of team data from FPL API.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Import sync service
        from rivals.services.team_sync_service import TeamSyncService

        try:
            success = TeamSyncService(team).sync_full_data()
            if success:
                # Refresh team from database
                team.refresh_from_db()
                return Response({
                    "success": True,
                    "message": "Team data synced successfully.",
                    "team": TeamSerializer(team).data,
                })
            else:
                return Response(
                    {"success": False, "error": "Failed to sync team data."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        except Exception as e:
            return Response(
                {"success": False, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class TeamTrackView(APIView):
    """
    Track or untrack a team for the authenticated user.
    POST to track, DELETE to untrack.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if user can track more teams
        if not request.user.can_track_team:
            return Response(
                {"error": "You have reached your tracking limit. Upgrade to premium."},
                status=status.HTTP_403_FORBIDDEN,
            )

        request.user.tracked_teams.get_or_create(team=team)

        # Sync the team data when tracking
        from rivals.services.team_sync_service import TeamSyncService
        TeamSyncService(team).sync_full_data()

        return Response({
            "success": True,
            "message": "Team is now being tracked.",
            "team": TeamSerializer(team).data,
        })

    def delete(self, request, team_id):
        try:
            team = Team.objects.get(id=team_id)
        except Team.DoesNotExist:
            return Response(
                {"error": "Team not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        request.user.tracked_teams.filter(team=team).delete()

        return Response({
            "success": True,
            "message": "Team untracked successfully.",
        })


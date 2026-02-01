import logging

from django.shortcuts import get_object_or_404
from rest_framework import generics, status, views
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rivals.models import PlannedTransfer, Player, Team, TransferPlan
from rivals.services.team_analysis_service import TeamAnalysisService

from .serializers import PlannedTransferSerializer, TransferPlanSerializer

logger = logging.getLogger(__name__)


class TransferPlanListCreateView(generics.ListCreateAPIView):
    """List create transfer plans for the authenticated user"""
    serializer_class = TransferPlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return TransferPlan.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class TransferPlanDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a specific transfer plan"""
    serializer_class = TransferPlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return TransferPlan.objects.filter(user=self.request.user)


class PlannedTransferCreateView(generics.CreateAPIView):
    """Add a transfer to a plan"""
    serializer_class = PlannedTransferSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        plan_id = self.kwargs.get("plan_id")
        plan = get_object_or_404(TransferPlan, id=plan_id, user=self.request.user)
        serializer.save(plan=plan)


class PlannedTransferDeleteView(generics.DestroyAPIView):
    """Remove a transfer from a plan"""
    queryset = PlannedTransfer.objects.all()
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Ensure user owns the plan
        return PlannedTransfer.objects.filter(plan__user=self.request.user)


class ProjectionsView(views.APIView):
    """Get 6-week projections for a team, optionally applying a plan"""
    permission_classes = [IsAuthenticated]

    def get(self, request, team_id):
        plan_id = request.query_params.get("plan_id")
        
        service = TeamAnalysisService()
        service.ensure_data()
        
        # Get base squad
        try:
            gw = service.current_gw
            team_picks = service.api.fetch_team_picks(team_id, gw)
            if not team_picks:
                return Response({"error": "Team picks not found"}, status=status.HTTP_404_NOT_FOUND)

            base_squad = team_picks.get("picks", [])

            # TODO: Apply planned transfers to base_squad if plan_id is provided
            # This logic will be complex as we need to project squad state forward

            # For MVP, just return current squad projections
            projections = []

            # Calculate projections for current squad
            total_expected_next_5 = 0
            for pick in base_squad:
                player = next((p for p in service.elements if p["id"] == pick["element"]), None)
                if player:
                    fd = service.calculate_fixture_difficulty(player["team"], 5)
                    expected = service.calculate_expected_points(player, fd)
                    total_expected_next_5 += expected

                    projections.append({
                        "id": player["id"],
                        "web_name": player["web_name"],
                        "expected_points_next_5": expected,
                        "fixture_difficulty": fd,
                    })

            return Response({
                "gameweek": gw,
                "total_expected_points_next_5": total_expected_next_5,
                "players": projections,
            })

        except Exception:
            logger.exception("Error generating projections for team %s (plan_id=%s)", team_id, plan_id)
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SuggestionsView(views.APIView):
    """Get transfer suggestions"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        position_type = request.query_params.get("position_type")  # 1=GKP, 2=DEF, etc
        max_price = request.query_params.get("max_price")
        
        if not position_type:
            return Response({"error": "position_type is required"}, status=status.HTTP_400_BAD_REQUEST)
            
        service = TeamAnalysisService()
        service.ensure_data()
        
        # Validate and parse inputs explicitly to avoid ValueError bubbling up
        try:
            pos_type_int = int(position_type)
        except ValueError:
            return Response({"error": "position_type must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            alternatives = service.get_top_alternatives_by_position(
                pos_type_int,
                exclude_ids=[],  # TODO: exclude current squad
                limit=20,
            )
        except Exception:
            logger.exception("Error fetching alternatives for position %s", pos_type_int)
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Filter by price if provided and validate
        if max_price:
            try:
                max_p = float(max_price)
            except ValueError:
                return Response({"error": "max_price must be a number"}, status=status.HTTP_400_BAD_REQUEST)

            alternatives = [a for a in alternatives if a.get("price") is not None and a["price"] <= max_p]

        return Response(alternatives[:10])

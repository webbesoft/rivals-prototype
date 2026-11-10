import logging
import math

from django.shortcuts import get_object_or_404
from rest_framework import generics, status, views
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from rivals.models import PlannedTransfer, TransferPlan
from rivals.services.decision_engine import DecisionEngine, DecisionUnavailable

from .serializers import PlannedTransferSerializer, TransferPlanSerializer

logger = logging.getLogger(__name__)


class TransferPlanListCreateView(generics.ListCreateAPIView):
    """List create transfer plans for the authenticated user"""

    serializer_class = TransferPlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return TransferPlan.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        try:
            engine = DecisionEngine(self.request.user).load()
        except DecisionUnavailable as error:
            raise ValidationError(str(error)) from error
        serializer.save(user=self.request.user, season=engine.context["season"])


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
    """Evaluate the manager's current squad, applying an owned, current-season plan."""

    permission_classes = [IsAuthenticated]

    def get(self, request, team_id):
        if request.user.team_id != team_id:
            return Response(
                {"error": "Open your own team to plan transfers."}, status=403
            )
        plan_id = request.query_params.get("plan_id")
        if plan_id:
            try:
                plan_id = int(plan_id)
            except (ValueError, TypeError):
                return Response({"error": "Choose a valid saved plan."}, status=400)
        plan = (
            get_object_or_404(
                TransferPlan, pk=plan_id, user=request.user, team=request.user.team
            )
            if plan_id
            else None
        )
        try:
            engine = DecisionEngine(request.user).load()
            inputs = engine.inputs()
            if plan:
                if plan.season != engine.context["season"]:
                    return Response(
                        {"error": "Create a plan for the current season."}, status=400
                    )
                transfers = list(plan.transfers.all())
                if any(
                    move.gameweek != engine.context["decision_gameweek"]
                    or not move.player_in_id
                    or not move.player_out_id
                    for move in transfers
                ):
                    return Response(
                        {
                            "error": "This comparison supports up to two transfers at the next deadline."
                        },
                        status=400,
                    )
                inputs["manual"] = [
                    [move.player_out.fpl_id, move.player_in.fpl_id]
                    for move in transfers
                ]
            return Response(engine.report(inputs))
        except (DecisionUnavailable, ValueError) as error:
            return Response({"error": str(error)}, status=400)
        except Exception:
            logger.exception("Transfer projection failed for user %s", request.user.pk)
            return Response(
                {"error": "Your projections are temporarily unavailable."}, status=503
            )


class SuggestionsView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            try:
                position = int(request.query_params.get("position_type", ""))
            except (ValueError, TypeError):
                raise ValueError("Choose a position between 1 and 4.") from None
            if position not in (1, 2, 3, 4):
                raise ValueError("Choose a position between 1 and 4.")
            max_price = request.query_params.get("max_price")
            try:
                ceiling = float(max_price) if max_price else None
            except (ValueError, TypeError):
                raise ValueError("Enter a valid maximum price.") from None
            if ceiling is not None and (not math.isfinite(ceiling) or ceiling < 0):
                raise ValueError("Enter a valid maximum price.")
            engine = DecisionEngine(request.user).load()
            report = engine.report()
            options = [
                option
                for option in report["options"]
                if any(
                    engine.players[move["in"]]["element_type"] == position
                    and (
                        ceiling is None
                        or engine.players[move["in"]].get("now_cost", 0) / 10 <= ceiling
                    )
                    for move in option["moves"]
                )
            ]
            return Response(options)
        except (DecisionUnavailable, ValueError) as error:
            return Response({"error": str(error)}, status=400)
        except Exception:
            logger.exception("Transfer suggestions failed for user %s", request.user.pk)
            return Response(
                {"error": "Transfer suggestions are temporarily unavailable."},
                status=503,
            )

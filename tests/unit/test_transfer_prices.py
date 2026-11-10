from decimal import Decimal

import pytest
from django.template import Context, Template

from rivals.models import Transfer


@pytest.mark.parametrize(
    "incoming,outgoing,difference,display",
    [
        ("75", "60", Decimal("1.5"), "+£1.5M"),
        ("50", "60", Decimal("-1.0"), "-£1.0M"),
        ("60", "60", Decimal("0"), "£0.0M"),
        ("61", "60", Decimal("0.1"), "+£0.1M"),
        ("60", "61", Decimal("-0.1"), "-£0.1M"),
        ("100", "45", Decimal("5.5"), "+£5.5M"),
        (None, "60", None, "N/A"),
        ("60", None, None, "N/A"),
    ],
)
def test_transfer_price_difference_uses_millions_and_preserves_sign(
    incoming, outgoing, difference, display
):
    transfer = Transfer(
        player_in_name="Incoming",
        player_out_name="Outgoing",
        player_in_cost=Decimal(incoming) if incoming is not None else None,
        player_out_cost=Decimal(outgoing) if outgoing is not None else None,
    )
    assert transfer.price_difference == difference
    assert transfer.price_difference_display == display
    html = Template("Price diff: {{ transfer.price_difference_display }}").render(
        Context({"transfer": transfer})
    )
    assert html == f"Price diff: {display}"


@pytest.mark.django_db
def test_saved_fpl_transfer_prices_display_without_reimport():
    from rivals.models import Team

    team = Team.objects.create(fpl_team_id="12345", team_name="My XI")
    transfer = Transfer.objects.create(
        team=team,
        gameweek=10,
        player_in_name="Incoming",
        player_out_name="Outgoing",
        player_in_cost=55,
        player_out_cost=70,
    )
    transfer.refresh_from_db()
    assert transfer.player_in_cost == Decimal("55.0")
    assert transfer.price_difference == Decimal("-1.5")
    assert transfer.transfer_summary == "Outgoing → Incoming (-£1.5M)"

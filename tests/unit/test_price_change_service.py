import pytest

from rivals.services.price_change_service import price_warning, transfer_timing


@pytest.mark.parametrize("value", [None, "bad", "NaN", "Infinity", "89.9", "-89.9"])
def test_missing_invalid_and_distant_progress_do_not_warn(value):
    assert price_warning({"price_change_percent": value}) is None


@pytest.mark.parametrize("value,direction", [("90", "rise"), ("-90", "fall")])
def test_current_progress_has_cautious_watch_label(value, direction):
    warning = price_warning({"price_change_percent": value})
    assert warning["label"] == f"Near price {direction}"
    assert "not a probability" in warning["detail"]


def test_only_next_update_projection_is_used_and_overrides_current_progress():
    player = {
        "price_change_percent": "102",
        "price_change_projections": [
            {"offset": 1, "projected_percent": "110"},
            {"offset": 0, "projected_percent": "85"},
        ],
    }
    assert price_warning(player) is None
    player["price_change_projections"][1]["projected_percent"] = "-101"
    assert price_warning(player)["label"] == "Price fall projected"


@pytest.mark.parametrize(
    "extra",
    [
        {"price_change_calibrating": True},
        {"price_change_locked_until": "2099-01-01T00:00:00Z"},
        {"price_change_locked_until": "invalid"},
    ],
)
def test_locked_or_calibrating_players_do_not_warn(extra):
    assert price_warning({"price_change_percent": "105", **extra}) is None


def test_expired_lock_and_missing_projection_allow_current_progress():
    assert (
        price_warning(
            {
                "price_change_percent": "95",
                "price_change_locked_until": "2000-01-01T00:00:00Z",
                "price_change_projections": None,
            }
        )["label"]
        == "Near price rise"
    )


@pytest.mark.parametrize("bank,budget_risk", [(None, False), (0, True), (0.1, False)])
def test_budget_risk_uses_remaining_bank_without_assuming_selling_loss(
    bank, budget_risk
):
    players = {1: {"price_change_percent": "-105"}, 2: {"price_change_percent": "105"}}
    option = {
        "moves": [{"out": 1, "in": 2, "out_name": "Seller", "in_name": "Buyer"}],
        "bank_after": bank,
    }
    notes = transfer_timing(option, players)
    assert any("Budget risk" in note for note in notes) == budget_risk
    assert any("could reduce the selling price" in note for note in notes)
    assert any("more expensive" in note for note in notes)


def test_combined_purchase_rises_can_exceed_bank():
    players = {pid: {"price_change_percent": "105"} for pid in range(1, 5)}
    option = {
        "moves": [
            {"out": 1, "in": 2, "out_name": "One", "in_name": "Two"},
            {"out": 3, "in": 4, "out_name": "Three", "in_name": "Four"},
        ],
        "bank_after": 0.1,
    }
    assert any("Budget risk" in note for note in transfer_timing(option, players))


def test_favourable_price_moves_are_explained_without_a_budget_warning():
    players = {1: {"price_change_percent": "105"}, 2: {"price_change_percent": "-105"}}
    option = {
        "moves": [{"out": 1, "in": 2, "out_name": "Seller", "in_name": "Buyer"}],
        "bank_after": 0,
    }
    notes = transfer_timing(option, players)
    assert any("cheaper" in note for note in notes)
    assert any("depending on your purchase price" in note for note in notes)
    assert not any("Budget risk" in note for note in notes)

from django import forms


class PlanningInputForm(forms.Form):
    free_transfers = forms.IntegerField(
        label="Available free transfers",
        required=False,
        min_value=0,
        max_value=5,
        widget=forms.NumberInput(
            attrs={"class": "border border-gray-300 p-2 w-20", "placeholder": "—"}
        ),
    )


class DecisionInputForm(PlanningInputForm):
    season = forms.CharField(widget=forms.HiddenInput)
    gameweek = forms.IntegerField(widget=forms.HiddenInput)
    bank = forms.DecimalField(
        label="Current bank (£m)",
        required=False,
        min_value=0,
        max_digits=5,
        decimal_places=1,
    )
    squad_confirmed = forms.BooleanField(
        label="These are my current 15 players", required=False
    )
    horizon = forms.TypedChoiceField(
        label="Plan ahead",
        choices=[(n, f"{n} gameweeks") for n in range(3, 7)],
        coerce=int,
        initial=5,
    )
    strategy = forms.ChoiceField(
        label="Approach",
        choices=[("protect", "Protect my lead"), ("chase", "Chase a gap")],
    )

    def __init__(self, *args, engine, **kwargs):
        from decimal import Decimal

        from rivals.models import MiniLeague, Team

        super().__init__(*args, **kwargs)
        self.engine = engine
        data = engine.inputs()
        self.initial.update(
            {
                "season": engine.context["season"],
                "gameweek": engine.context["decision_gameweek"],
                "bank": (
                    Decimal(data["bank"]) / 10 if data["bank"] is not None else None
                ),
                "free_transfers": data["free_transfers"],
                "squad_confirmed": data["confirmed"],
                "horizon": data["horizon"],
                "strategy": data["strategy"],
            }
        )
        leagues = MiniLeague.objects.filter(
            user_minileagues__user=engine.user,
            user_minileagues__active=True,
            active=True,
        ).distinct()
        self.fields["target_league"] = forms.ModelChoiceField(
            label="Target mini-league", queryset=leagues, required=False
        )
        chosen = self.data.get("target_league") if self.is_bound else data["league_id"]
        try:
            chosen_id = int(chosen) if chosen else None
        except (ValueError, TypeError):
            chosen_id = None
        rivals = (
            Team.objects.filter(
                mini_league_teams__mini_league_id=chosen_id,
                mini_league_teams__season=engine.context["season"],
            )
            .exclude(pk=engine.team.pk)
            .distinct()
            if leagues.filter(pk=chosen_id).exists()
            else Team.objects.none()
        )
        self.fields["target_league"].widget.attrs[
            "onchange"
        ] = "this.form.elements.target_rival.value = ''; this.form.requestSubmit();"
        self.fields["target_rival"] = forms.ModelChoiceField(
            label="Key rival",
            queryset=rivals,
            required=False,
            empty_label="Auto: league leader",
        )
        self.initial.update(target_league=chosen_id, target_rival=data["rival_id"])
        self.squad_rows = []
        # Slots retain their position when managers update post-deadline holdings.
        for index, pid in enumerate(data["ids"]):
            pos = engine.players[pid]["element_type"]
            choices = [
                (
                    player_id,
                    f"{engine.name(player_id)} · £{player.get('now_cost', 0) / 10:.1f}m",
                )
                for player_id, player in engine.players.items()
                if player["element_type"] == pos
            ]
            self.fields[f"owned_{index}"] = forms.TypedChoiceField(
                choices=sorted(choices, key=lambda item: item[1]),
                coerce=int,
                label="Player",
            )
            self.fields[f"sell_{index}"] = forms.DecimalField(
                label="Selling price (£m)",
                required=False,
                decimal_places=1,
                min_value=0,
                max_value=100,
            )
            self.fields[f"owned_{index}"].widget.attrs[
                "onchange"
            ] = f"this.form.elements.sell_{index}.value = ''; this.form.elements.bank.value = ''; this.form.elements.free_transfers.value = ''; this.form.elements.squad_confirmed.checked = false; this.form.elements.out_1.value = ''; this.form.elements.in_1.value = ''; this.form.elements.out_2.value = ''; this.form.elements.in_2.value = '';"
            self.initial[f"owned_{index}"] = pid
            self.initial[f"sell_{index}"] = (
                Decimal(data["sales"][str(pid)]) / 10
                if str(pid) in data["sales"]
                else None
            )
            self.squad_rows.append(
                {
                    "player_field": self[f"owned_{index}"],
                    "price_field": self[f"sell_{index}"],
                }
            )
        outgoing_ids = data["ids"]
        if self.is_bound:
            outgoing_ids = [
                int(self.data[f"owned_{index}"])
                for index in range(15)
                if str(self.data.get(f"owned_{index}", "")).isdigit()
                and int(self.data[f"owned_{index}"]) in engine.players
            ]
        for index in (1, 2):
            self.fields[f"out_{index}"] = forms.TypedChoiceField(
                label=f"Move {index}: sell",
                required=False,
                choices=[("", "No move")]
                + [(pid, engine.name(pid)) for pid in outgoing_ids],
                coerce=int,
                empty_value=None,
            )
            self.fields[f"in_{index}"] = forms.TypedChoiceField(
                label="Buy",
                required=False,
                choices=[("", "No move")]
                + sorted(
                    [(pid, engine.name(pid)) for pid in engine.players],
                    key=lambda item: item[1],
                ),
                coerce=int,
                empty_value=None,
            )
            if len(data["manual"]) >= index:
                self.initial[f"out_{index}"], self.initial[f"in_{index}"] = data[
                    "manual"
                ][index - 1]
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput)):
                field.widget.attrs.setdefault(
                    "class", "border border-gray-300 p-2 max-w-full"
                )

    def clean(self):
        cleaned = super().clean()
        engine = self.engine
        if (
            cleaned.get("season") != engine.context["season"]
            or cleaned.get("gameweek") != engine.context["decision_gameweek"]
        ):
            raise forms.ValidationError(
                "The deadline has changed. Reload your plan before confirming these details."
            )
        ids = [cleaned.get(f"owned_{index}") for index in range(15)]
        if all(pid is not None for pid in ids):
            try:
                engine.validate_squad(ids)
            except ValueError as error:
                raise forms.ValidationError(str(error))
        sales = {}
        for index, pid in enumerate(ids):
            price = cleaned.get(f"sell_{index}")
            if price is not None and pid in engine.players:
                tenths = int(price * 10)
                if tenths > engine.players[pid].get("now_cost", 0):
                    self.add_error(
                        f"sell_{index}",
                        "Use the selling price shown in FPL; it cannot exceed the current purchase price.",
                    )
                sales[str(pid)] = tenths
        moves = []
        for index in (1, 2):
            outgoing, incoming = cleaned.get(f"out_{index}"), cleaned.get(f"in_{index}")
            if bool(outgoing) != bool(incoming):
                self.add_error(
                    f"in_{index}",
                    "Choose both players for this move, or leave both blank.",
                )
            if outgoing and incoming:
                moves.append([outgoing, incoming])
        cleaned["ids"], cleaned["sales"], cleaned["manual"] = ids, sales, moves
        return cleaned

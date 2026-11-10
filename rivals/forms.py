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

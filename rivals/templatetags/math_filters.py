from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def subtract(value, arg):
    """Subtract numeric template values without truncating projected points."""
    try:
        return Decimal(str(value)) - Decimal(str(arg))
    except (InvalidOperation, ValueError, TypeError):
        return ""


@register.filter
def sub(value, arg):
    """Subtracts the arg from the value."""
    try:
        return int(value) - int(arg)
    except (ValueError, TypeError):
        return ""


@register.filter(name="abs")
def absolute(value):
    """Returns the absolute value of a number."""
    try:
        return abs(int(value))
    except (ValueError, TypeError):
        return ""

from django import template

register = template.Library()


@register.filter
def date_time(value):
    # Your filter logic here
    return value.strftime("%Y-%m-%d %H:%M:%S")

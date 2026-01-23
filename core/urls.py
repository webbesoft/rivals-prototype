from django.contrib import admin
from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("", views.index, name="index"),
    path("/health", views.health_check, name="health_check"),
]

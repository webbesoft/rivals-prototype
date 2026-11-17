"""
URL configuration for rivalspy project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from debug_toolbar.toolbar import debug_toolbar_urls
from django.contrib import admin
from django.urls import include, path
from accounts.passwordless_views import admin_login
from accounts.views import login_view

urlpatterns = [
    path("admin/login/", admin_login),
    path("accounts/", include("allauth.socialaccount.providers.google.urls")),
    # Allauth's social flow uses this reverse name. Local account endpoints stay ours.
    path("accounts/login/", login_view, name="account_login"),
    path("", include("rivals_mcp.urls", namespace="oauth2_provider")),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls", namespace="accounts")),
    path("", include("core.urls", namespace="core")),
    path("dashboard/", include("rivals.urls", namespace="rivals")),
    path("api/", include("api.urls", namespace="api")),
] + debug_toolbar_urls()

"""
ASGI config for rivalspy project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rivalspy.settings")

django_application = get_asgi_application()

from rivals_mcp.server import create_app

mcp_application = create_app()


async def application(scope, receive, send):
    if scope["type"] == "lifespan" or (
        scope["type"] == "http" and scope.get("path") == "/mcp"
    ):
        return await mcp_application(scope, receive, send)
    return await django_application(scope, receive, send)

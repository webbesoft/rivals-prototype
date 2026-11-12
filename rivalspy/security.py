"""Validate runtime deployment settings without including secrets in errors."""

import ipaddress
from email.utils import parseaddr
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.utils.http import is_same_domain


def validate_production_settings(config):
    problems = []
    secret = config["SECRET_KEY"]
    if (
        len(secret) < 32
        or len(set(secret)) < 5
        or any(
            secret == (secret[:size] * (len(secret) // size + 1))[: len(secret)]
            for size in range(1, len(secret) // 2 + 1)
        )
        or secret.startswith("django-insecure-")
        or secret
        in (
            "secret-key-should-be-changed",
            "local-development-only-change-for-production",
            "your-secret-key-here",
        )
    ):
        problems.append(
            "SECRET_KEY must be configured with an independently generated key of at least 32 characters; development placeholders are not allowed"
        )

    hosts = config["ALLOWED_HOSTS"]
    valid_hosts = bool(hosts) and not any(
        "*" in host or host.startswith(".") for host in hosts
    )
    if not hosts:
        problems.append(
            "ALLOWED_HOSTS is missing; list the permitted application hosts"
        )
    elif any("*" in host or host.startswith(".") for host in hosts):
        problems.append(
            "ALLOWED_HOSTS contains a wildcard; list the permitted application hosts explicitly"
        )

    try:
        if any(character.isspace() for character in config["SITE_URL"]):
            raise ValueError
        site = urlsplit(config["SITE_URL"])
        # Accessing port also validates malformed or out-of-range ports.
        site.port
        if (
            site.scheme != "https"
            or not site.hostname
            or site.username is not None
            or site.password is not None
            or site.query
            or site.fragment
            or site.path not in ("", "/")
        ):
            raise ValueError
        if site.hostname == "localhost" or site.hostname.endswith(".localhost"):
            raise ValueError
        try:
            address = ipaddress.ip_address(site.hostname)
        except ValueError:
            address = None
        if address and (not address.is_global):
            raise ValueError
        if valid_hosts and not any(
            is_same_domain(site.hostname, host.strip("[]")) for host in hosts
        ):
            raise ValueError
    except ValueError:
        problems.append(
            "SITE_URL must be the public HTTPS origin for a host in ALLOWED_HOSTS"
        )

    sender = config["DEFAULT_FROM_EMAIL"]
    try:
        if "\r" in sender or "\n" in sender:
            raise ValueError
        _, address = parseaddr(sender)
        validate_email(address)
        if address == "noreply@yoursaas.com":
            raise ValueError
    except (ValidationError, ValueError):
        problems.append("DEFAULT_FROM_EMAIL must be a valid configured sender")

    if config["EMAIL_USE_TLS"] and config["EMAIL_USE_SSL"]:
        problems.append("Enable only one of EMAIL_USE_TLS and EMAIL_USE_SSL")
    if (
        config["EMAIL_BACKEND"] == "django.core.mail.backends.smtp.EmailBackend"
        and config["EMAIL_HOST"] not in ("localhost", "127.0.0.1", "::1")
        and not (config["EMAIL_USE_TLS"] or config["EMAIL_USE_SSL"])
    ):
        problems.append(
            "Remote SMTP connections require EMAIL_USE_TLS or EMAIL_USE_SSL"
        )
    if problems:
        raise ImproperlyConfigured(
            "Unsafe production configuration: " + "; ".join(problems)
        )

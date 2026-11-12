import os
from pathlib import Path
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

from rivalspy.security import validate_production_settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def production():
    return {
        "SECRET_KEY": "aB9!cD8@eF7#gH6$iJ5%kL4^mN3&oP2*qR1(sT0)uV9_wX8+yZ7",
        "ALLOWED_HOSTS": ["rivals.example"],
        "SITE_URL": "https://rivals.example",
        "DEFAULT_FROM_EMAIL": "RivalSpy <briefings@rivals.example>",
        "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "EMAIL_HOST": "smtp.rivals.example",
        "EMAIL_USE_TLS": True,
        "EMAIL_USE_SSL": False,
    }


def test_valid_production_settings(production):
    validate_production_settings(production)


def test_existing_44_character_key_does_not_require_rotation(production):
    production["SECRET_KEY"] = "Jc4d8QV61pRexWyozGkhqP3wGm9+0fRXbL6Xzj6Qh3A="
    assert len(production["SECRET_KEY"]) == 44
    validate_production_settings(production)


def test_invalid_host_list_does_not_report_a_valid_site_url_as_broken(production):
    production["ALLOWED_HOSTS"] = ["*"]
    with pytest.raises(ImproperlyConfigured) as error:
        validate_production_settings(production)
    assert "ALLOWED_HOSTS" in str(error.value)
    assert "SITE_URL" not in str(error.value)


def test_explicit_production_command_rejects_missing_injected_secret_without_traceback(
    production,
):
    env = runtime_environment(production)
    env.pop("SECRET_KEY")
    env["DJANGO_SETTINGS_MODULE"] = "rivalspy.settings"
    env["PYTHON_DOTENV_DISABLED"] = "1"
    script = (
        "from django.core.management import execute_from_command_line; "
        "execute_from_command_line(['manage.py','check_production_config'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "CommandError: Unsafe production configuration" in result.stderr
    assert "SECRET_KEY" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.django_db
def test_worker_health_waits_for_heartbeat_without_real_sleep(monkeypatch):
    from django.core.management import call_command
    from django.utils import timezone
    from rivals.models import SyncState

    clock = [0]
    monkeypatch.setattr(
        "rivals.management.commands.check_fpl_worker.time.monotonic", lambda: clock[0]
    )

    def worker_starts(seconds):
        clock[0] += seconds
        SyncState.objects.create(
            key="worker", kind="worker", last_success_at=timezone.now()
        )

    monkeypatch.setattr(
        "rivals.management.commands.check_fpl_worker.time.sleep", worker_starts
    )
    call_command("check_fpl_worker", wait=5)
    assert clock[0] == 2


@pytest.mark.django_db
def test_worker_health_wait_is_bounded_and_fails_closed(monkeypatch):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    clock = [0]
    monkeypatch.setattr(
        "rivals.management.commands.check_fpl_worker.time.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(
        "rivals.management.commands.check_fpl_worker.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    with pytest.raises(CommandError, match="No recent"):
        call_command("check_fpl_worker", wait=3)
    assert clock[0] == 3


@pytest.mark.parametrize("exit_code", [0, 9])
def test_post_deploy_checks_new_worker_and_preserves_failure(tmp_path, exit_code):
    log = tmp_path / "commands"
    kamal = tmp_path / "kamal"
    kamal.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$COMMAND_LOG"\nexit "$EXIT_CODE"\n'
    )
    kamal.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "COMMAND_LOG": str(log),
        "EXIT_CODE": str(exit_code),
        "KAMAL_VERSION": "release-under-test",
    }
    result = subprocess.run(
        ["/bin/bash", str(ROOT / ".kamal/hooks/post-deploy")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == exit_code
    command = log.read_text()
    assert "--reuse --primary --roles job --version release-under-test" in command
    assert "check_fpl_worker --wait 60" in command
    assert "migrate" not in command


@pytest.mark.parametrize(
    "setting,value",
    [
        ("SECRET_KEY", ""),
        ("SECRET_KEY", "secret-key-should-be-changed"),
        ("SECRET_KEY", "x" * 60),
        ("SECRET_KEY", "django-insecure-" + "abcdef" * 10),
        ("ALLOWED_HOSTS", []),
        ("ALLOWED_HOSTS", ["*"]),
        ("ALLOWED_HOSTS", ["*.example"]),
        ("SITE_URL", "http://rivals.example"),
        ("SITE_URL", "https://localhost"),
        ("SITE_URL", "https://127.0.0.1"),
        ("SITE_URL", "https://[::1]"),
        ("SITE_URL", "https://0.0.0.0"),
        ("SITE_URL", "https://attacker.example"),
        ("SITE_URL", "https://user:secret@rivals.example"),
        ("SITE_URL", "https://rivals.example/account"),
        ("SITE_URL", "https://rivals.example?token=secret"),
        ("SITE_URL", "https://rivals.example#secret"),
        ("SITE_URL", "https://rivals.example:bad"),
        ("SITE_URL", "https://rivals.example\n"),
        ("DEFAULT_FROM_EMAIL", ""),
        ("DEFAULT_FROM_EMAIL", "noreply@yoursaas.com"),
        ("DEFAULT_FROM_EMAIL", "briefings@rivals.example\r\nBcc: someone@example.com"),
    ],
)
def test_unsafe_production_settings_rejected_without_echoing_values(
    production, setting, value
):
    production[setting] = value
    with pytest.raises(ImproperlyConfigured) as error:
        validate_production_settings(production)
    assert setting in str(error.value)
    assert "secret@" not in str(error.value)
    assert "someone@example.com" not in str(error.value)


def test_smtp_cannot_send_credentials_over_plaintext(production):
    production["EMAIL_USE_TLS"] = False
    with pytest.raises(ImproperlyConfigured, match="Remote SMTP"):
        validate_production_settings(production)
    production["EMAIL_HOST"] = "localhost"
    validate_production_settings(production)
    production.update(EMAIL_USE_TLS=True, EMAIL_USE_SSL=True)
    with pytest.raises(ImproperlyConfigured, match="only one"):
        validate_production_settings(production)


def runtime_environment(production):
    env = {
        key: ",".join(value) if isinstance(value, list) else str(value)
        for key, value in production.items()
    }
    env.update(DEBUG="False", SENTRY_DSN="", PYTHONDONTWRITEBYTECODE="1")
    return env


def test_settings_import_does_not_validate_before_runtime_injection(production):
    env = runtime_environment(production)
    env.pop("SECRET_KEY")
    script = "import dotenv; dotenv.load_dotenv=lambda:False; import rivalspy.settings"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_production_runtime_import_and_secretless_static_build(production):
    script = "import dotenv; dotenv.load_dotenv=lambda:False; import rivalspy.settings"
    runtime = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=runtime_environment(production),
        capture_output=True,
        text=True,
    )
    assert runtime.returncode == 0, runtime.stderr
    build = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv=['manage.py','collectstatic']; " + script,
        ],
        cwd=ROOT,
        env={"DEBUG": "False", "SENTRY_DSN": ""},
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr


@pytest.mark.parametrize("failure,expected", [("check", 1), ("migrate", 2), ("", 3)])
def test_web_startup_never_serves_before_checks_and_migrations(
    tmp_path, failure, expected
):
    log = tmp_path / "commands"
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'case "$*" in\n'
        '  "manage.py check_production_config"*) [ "$FAIL_STEP" != check ] || exit 7;;\n'
        '  "manage.py migrate "*) [ "$FAIL_STEP" != migrate ] || exit 8;;\n'
        "esac\n"
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "COMMAND_LOG": str(log),
        "FAIL_STEP": failure,
        "PORT": "8123",
    }
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "bin/start-web")],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    commands = log.read_text().splitlines()
    assert len(commands) == expected
    if failure:
        assert result.returncode != 0
        assert not any("uvicorn" in command for command in commands)
    else:
        assert result.returncode == 0
        assert commands[-1].startswith("-m uvicorn ")
        assert "--port 8123" in commands[-1]


@pytest.mark.parametrize("failure", ["configuration", "python", "javascript", ""])
def test_release_gate_runs_both_suites_and_propagates_failures(tmp_path, failure):
    log = tmp_path / "commands"
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "uv %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'case "$*" in\n'
        '  "python install "*) exit 0;;\n'
        '  *"check --deploy"*) [ "$DEBUG" = False ] && [ -z "$SENTRY_DSN" ] || exit 99; '
        '[ "$FAIL_STEP" != configuration ] || exit 6; exit 0;;\n'
        "esac\n"
        '[ "$DEBUG" = True ] && [ -z "$SENTRY_DSN" ] && [ -z "${PYTEST_ADDOPTS:-}" ] || exit 99\n'
        'case "$*" in *pytest*) [ "$FAIL_STEP" != python ] || exit 7;; esac\n'
    )
    ruby = tmp_path / "ruby"
    ruby.write_text('#!/bin/sh\n[ "$FAIL_STEP" != configuration ] || exit 6\n')
    ruby.chmod(0o755)
    node = tmp_path / "node"
    node.write_text(
        '#!/bin/sh\nprintf "node %s\\n" "$*" >> "$COMMAND_LOG"\n'
        '[ "$FAIL_STEP" != javascript ] || exit 8\n'
    )
    for executable in (uv, node):
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "COMMAND_LOG": str(log),
        "FAIL_STEP": failure,
        "DEBUG": "False",
        "SENTRY_DSN": "",
        "PYTEST_ADDOPTS": "--collect-only",
    }
    result = subprocess.run(
        ["/bin/bash", str(ROOT / ".kamal/hooks/pre-deploy")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    commands = log.read_text().splitlines()
    if failure == "configuration":
        assert result.returncode == 6
        assert not any("pytest" in command for command in commands)
        return
    assert any("python -m pytest" in command for command in commands)
    if failure == "python":
        assert result.returncode == 7
        assert not any(command.startswith("node ") for command in commands)
    else:
        assert any(command.startswith("node --test ") for command in commands)
        assert result.returncode == (8 if failure == "javascript" else 0)
    if not failure:
        assert any("makemigrations --check" in command for command in commands)


@pytest.mark.parametrize("key", ["abcde12345" * 6, ("abcdefghijk" * 6)[:60]])
def test_repeated_secret_patterns_are_rejected(production, key):
    production["SECRET_KEY"] = key
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        validate_production_settings(production)


@pytest.mark.parametrize(
    "address", ["10.1.2.3", "192.168.1.1", "169.254.1.2", "100.64.0.1", "fc00::1"]
)
def test_non_global_site_addresses_rejected(production, address):
    production["ALLOWED_HOSTS"] = [address]
    production["SITE_URL"] = "https://" + (
        f"[{address}]" if ":" in address else address
    )
    with pytest.raises(ImproperlyConfigured, match="SITE_URL"):
        validate_production_settings(production)


def test_explicit_global_address_accepted_and_dot_hosts_rejected(production):
    production.update(ALLOWED_HOSTS=["8.8.8.8"], SITE_URL="https://8.8.8.8")
    validate_production_settings(production)
    production.update(
        ALLOWED_HOSTS=[".example.com"], SITE_URL="https://rivals.example.com"
    )
    with pytest.raises(ImproperlyConfigured, match="ALLOWED_HOSTS"):
        validate_production_settings(production)

"""Exercise the deployed environment through Kamal's actual dotenv parser."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from cryptography.fernet import Fernet
from dotenv import dotenv_values
import pytest

ROOT = Path(__file__).resolve().parents[2]
FLAGS = ("AI_GENERATION_ENABLED", "MCP_ENABLED", "PASSWORDLESS_AUTH_ENABLED")
OPTIONAL = (
    *FLAGS,
    "AI_CREDENTIAL_KEYS",
    "AI_CREDENTIAL_ACTIVE_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "MCP_ALLOWED_ORIGINS",
)


@pytest.fixture(scope="module")
def ruby_environment():
    if not shutil.which("ruby"):
        pytest.skip("Ruby and Kamal 2.12.0 are required for deployment tests")
    # Exclude all inherited app settings and credentials.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "GEM_HOME", "GEM_PATH")
        if key in os.environ
    }
    environment["RUBYOPT"] = "-W0"
    result = subprocess.run(
        [
            "ruby",
            "-e",
            'begin; gem "kamal", "2.12.0"; require "kamal"; '
            "rescue LoadError; exit 78; end",
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode == 78:
        pytest.skip("Kamal 2.12.0 is required for deployment tests")
    assert result.returncode == 0, result.stderr
    return environment


@pytest.mark.parametrize("flag_value", ["True", "False", None, ""])
def test_kamal_passes_production_settings_to_django(
    tmp_path, ruby_environment, flag_value
):
    (tmp_path / "config").mkdir()
    (tmp_path / ".kamal").mkdir()
    shutil.copy(ROOT / "config/deploy.yml", tmp_path / "config/deploy.yml")
    shutil.copy(ROOT / ".kamal/secrets", tmp_path / ".kamal/secrets")
    source = dict(dotenv_values(ROOT / ".env.production.example"))
    source.update(
        SECRET_KEY="deployment-test-only-57a624b380fe49f8b23dca6072d9e4a5",
        AI_CREDENTIAL_KEYS=json.dumps({"v2": Fernet.generate_key().decode()}),
        AI_CREDENTIAL_ACTIVE_KEY="v2",
        GOOGLE_CLIENT_ID="test-client-id",
        GOOGLE_CLIENT_SECRET="test-client-secret",
        MCP_ALLOWED_ORIGINS="https://assistant.example,https://rivals.webbe.dev",
    )
    if flag_value in (None, ""):
        for key in OPTIONAL:
            if flag_value is None:
                source.pop(key, None)
            else:
                source[key] = ""
        expected = dict.fromkeys(OPTIONAL, "")
        expected.update(
            AI_GENERATION_ENABLED="True",
            MCP_ENABLED="True",
            PASSWORDLESS_AUTH_ENABLED="False",
            AI_CREDENTIAL_ACTIVE_KEY="v1",
            MCP_ALLOWED_ORIGINS=source["SITE_URL"],
        )
    else:
        source.update(dict.fromkeys(FLAGS, flag_value))
        expected = {key: source[key] for key in OPTIONAL}
    source["FPL_PLAYER_MODEL"] = "decision-v12-calibrated"
    expected["FPL_PLAYER_MODEL"] = "decision-v12-calibrated"
    (tmp_path / ".env.production").write_text(
        "\n".join(f"{key}='{value}'" for key, value in source.items()) + "\n"
    )
    result = subprocess.run(
        ["ruby", "-"],
        input="""
gem "kamal", "2.12.0"
require "kamal"
require "json"
config = Kamal::Configuration.create_from(config_file: Pathname.new("config/deploy.yml"))
environments = config.roles.to_h do |role|
  [role.name, role.hosts.map { |host| role.env(host).to_h }]
end
puts JSON.generate(environments)
""",
        cwd=tmp_path,
        env=ruby_environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    environments = json.loads(result.stdout)
    assert set(environments) == {"web", "job"}
    for hosts in environments.values():
        for environment in hosts:
            for key, value in expected.items():
                assert environment[key] == value, f"Kamal corrupted {key}"
            # Import the real Django settings with only the container environment.
            check = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import dotenv
dotenv.load_dotenv = lambda: False
import django
django.setup()
from django.conf import settings
from rivals.services.ai_credentials import configured
print(__import__('json').dumps({
    'AI_GENERATION_ENABLED': settings.AI_GENERATION_ENABLED,
    'MCP_ENABLED': settings.MCP_ENABLED,
    'PASSWORDLESS_AUTH_ENABLED': settings.PASSWORDLESS_AUTH_ENABLED,
    'FPL_PLAYER_MODEL': settings.FPL_PLAYER_MODEL,
    'keyring_configured': configured(),
}))
""",
                ],
                cwd=ROOT,
                env={
                    **{key: str(value) for key, value in environment.items()},
                    "DJANGO_SETTINGS_MODULE": "rivalspy.settings",
                    "PYTHON_DOTENV_DISABLED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SENTRY_DSN": "",
                },
                capture_output=True,
                text=True,
            )
            assert check.returncode == 0, check.stderr
            runtime = json.loads(check.stdout)
            for key in FLAGS:
                assert runtime[key] == (expected[key] == "True")
            assert runtime["keyring_configured"] == bool(expected["AI_CREDENTIAL_KEYS"])
            assert runtime["FPL_PLAYER_MODEL"] == expected["FPL_PLAYER_MODEL"]

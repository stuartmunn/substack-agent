"""Tests for the Dockerfile and compose file (SA-15 acceptance criteria)."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = "/data"
SERVICE = "humblebrag"


def _dockerfile_lines() -> list[str]:
    """Return non-comment Dockerfile lines."""
    return [
        line.strip()
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _service() -> dict[str, Any]:
    """Return the compose service definition."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return compose["services"][SERVICE]


def test_dockerfile_uses_pinned_slim_base() -> None:
    """AC1: slim base image pinned to a specific version (DKR-01)."""
    base = _dockerfile_lines()[0]
    assert re.fullmatch(r"FROM python:3\.12\.\d+-slim", base), base


def test_dockerfile_runs_as_non_root() -> None:
    """AC1: the last USER instruction is not root."""
    users = [line.split()[1] for line in _dockerfile_lines() if line.startswith("USER")]
    assert users, "no USER instruction"
    assert users[-1] not in {"root", "0"}


def test_dockerfile_owns_data_dir_for_non_root_user() -> None:
    """AC4: /data exists in the image and is writable by the app user."""
    text = "\n".join(_dockerfile_lines())
    assert "mkdir /data" in text
    assert "chown humblebrag:humblebrag /data" in text


def test_dockerfile_does_not_bake_in_secrets() -> None:
    """AC1: no secrets are copied or set in the image (DKR-05)."""
    text = (ROOT / "Dockerfile").read_text()
    assert "secrets" not in text
    assert ".env" not in text


def test_dockerfile_exposes_no_ports() -> None:
    """AC3: no EXPOSE instruction."""
    assert not [line for line in _dockerfile_lines() if line.startswith("EXPOSE")]


def test_compose_restart_policy() -> None:
    """AC2: restart: unless-stopped."""
    assert _service()["restart"] == "unless-stopped"


def test_compose_mounts_persistent_volume_at_data() -> None:
    """AC2/AC4: a named volume (not a container layer) is mounted at /data."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    volume_name, target = _service()["volumes"][0].split(":")
    assert target == DATA_DIR
    assert volume_name in compose["volumes"]


def test_compose_defines_docker_secrets_wired_to_file_variables() -> None:
    """AC2: each secret is defined, attached and read via its *_FILE variable."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = _service()
    expected = {
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN_FILE",
        "anthropic_api_key": "ANTHROPIC_API_KEY_FILE",
        "substack_cookie": "SUBSTACK_COOKIE_FILE",
    }
    for secret, file_var in expected.items():
        assert secret in compose["secrets"]
        assert secret in service["secrets"]
        assert service["environment"][file_var] == f"/run/secrets/{secret}"


def test_compose_passes_no_secret_values_through_environment() -> None:
    """AC2: secret values are never given as plain environment variables."""
    environment = _service()["environment"]
    for name in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "SUBSTACK_COOKIE"):
        assert name not in environment


def test_compose_exposes_no_ports() -> None:
    """AC3: no ports or expose keys on the service."""
    service = _service()
    assert "ports" not in service
    assert "expose" not in service

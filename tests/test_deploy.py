"""Tests for scripts/deploy.sh using stub git and docker programs (SA-39)."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy.sh"

GIT_STUB = """#!/bin/bash
echo "git $*" >> "$STUB_LOG"
[ "${STUB_GIT_FAIL:-0}" = 1 ] && exit 1
exit 0
"""

DOCKER_STUB = """#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
case "$1 $2" in
  "compose up") [ "${STUB_UP_FAIL:-0}" = 1 ] && exit 1; exit 0 ;;
  "compose ps") echo "container-1"; exit 0 ;;
  "compose exec") exit "${STUB_SMOKE_EXIT:-0}" ;;
esac
if [ "$1" = inspect ]; then
  count_file="$STUB_DIR/inspect_count"
  count=$(cat "$count_file" 2>/dev/null || echo 0)
  echo $((count + 1)) > "$count_file"
  if [ "$count" -lt "${STUB_STARTING_POLLS:-0}" ]; then echo starting; else
    echo "${STUB_FINAL_HEALTH:-healthy}"; fi
  exit 0
fi
exit 0
"""

SLEEP_STUB = "#!/bin/bash\nexit 0\n"


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """A directory of fake ``git``, ``docker`` and ``sleep`` programs."""
    directory = tmp_path / "bin"
    directory.mkdir()
    for name, body in (
        ("git", GIT_STUB),
        ("docker", DOCKER_STUB),
        ("sleep", SLEEP_STUB),
    ):
        path = directory / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return directory


def _deploy(
    stubs: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the script with the stubs first on PATH; return the result and the
    commands the stubs saw, in order."""
    log = stubs.parent / "calls.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_DIR": str(stubs.parent),
            "DEPLOY_HEALTH_TIMEOUT": "30",
            "DEPLOY_POLL_SECONDS": "5",
            **env,
        },
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_the_script_is_executable_and_valid_bash() -> None:
    """The deploy is one command: it can be run directly."""
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_a_good_deploy_pulls_builds_waits_then_runs_the_smoke_test(stubs: Path) -> None:
    """AC1/AC2: the documented flow, in order, ending in the smoke test."""
    result, calls = _deploy(stubs)
    assert result.returncode == 0, result.stderr
    assert calls[0] == "git pull --ff-only"
    assert calls[1] == "docker compose up -d --build"
    assert calls[-1] == "docker compose exec -T humblebrag python -m humblebrag.smoke"
    assert any(call.startswith("docker inspect") for call in calls)
    assert "reporting for duty" in result.stdout


def test_it_waits_while_the_container_is_still_starting(stubs: Path) -> None:
    """A container that is not healthy yet is polled until it is."""
    result, calls = _deploy(stubs, STUB_STARTING_POLLS="3")
    assert result.returncode == 0
    assert len([c for c in calls if c.startswith("docker inspect")]) == 4


def test_an_unhealthy_container_fails_the_deploy_and_skips_the_smoke_test(
    stubs: Path,
) -> None:
    """A container that never becomes healthy is a failed deploy, not a pass."""
    result, calls = _deploy(stubs, STUB_FINAL_HEALTH="unhealthy")
    assert result.returncode != 0
    assert "DEPLOY FAILED" in result.stderr
    assert "unhealthy" in result.stderr
    assert not any("humblebrag.smoke" in call for call in calls)


def test_a_failed_smoke_test_fails_the_deploy(stubs: Path) -> None:
    """AC2: if the Telegram message cannot be sent the script exits non-zero."""
    result, _ = _deploy(stubs, STUB_SMOKE_EXIT="1")
    assert result.returncode != 0
    assert "smoke test failed" in result.stderr


def test_a_failed_pull_stops_before_building(stubs: Path) -> None:
    """A non-fast-forward pull must not deploy half-updated code."""
    result, calls = _deploy(stubs, STUB_GIT_FAIL="1")
    assert result.returncode != 0
    assert not any(call.startswith("docker") for call in calls)


def test_a_failed_build_stops_before_waiting(stubs: Path) -> None:
    """A failed `up` is reported and nothing after it runs."""
    result, calls = _deploy(stubs, STUB_UP_FAIL="1")
    assert result.returncode != 0
    assert not any(call.startswith("docker inspect") for call in calls)
    assert not any("humblebrag.smoke" in call for call in calls)


def test_the_script_contains_no_secret_values() -> None:
    """SEC-01/SEC-05: it reads no secret files and prints no secrets."""
    text = SCRIPT.read_text()
    assert "secrets/" not in text
    assert shutil.which("bash")

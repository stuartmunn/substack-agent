"""Tests for the heartbeat and healthcheck (SA-16 AC2 and AC3)."""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import pytest

from humblebrag import heartbeat
from humblebrag.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_MAX_AGE_SECONDS,
    check_health,
    heartbeat_is_fresh,
    heartbeat_loop,
    touch_heartbeat,
)

ROOT = Path(__file__).resolve().parent.parent
TWO_MINUTES = 120


def _set_age(path: Path, seconds: float) -> None:
    """Make ``path`` look ``seconds`` old."""
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_touch_heartbeat_creates_file(tmp_path: Path) -> None:
    """AC2: touching creates the heartbeat file."""
    path = tmp_path / "hb"
    touch_heartbeat(path)
    assert path.is_file()


def test_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    """AC2: a recently touched heartbeat passes the healthcheck."""
    path = tmp_path / "hb"
    touch_heartbeat(path)
    assert check_health(path) == 0


def test_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    """AC3: no heartbeat file means unhealthy."""
    assert check_health(tmp_path / "hb") != 0


def test_stale_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    """AC3: a heartbeat older than the max age fails the healthcheck."""
    path = tmp_path / "hb"
    touch_heartbeat(path)
    _set_age(path, HEARTBEAT_MAX_AGE_SECONDS + 5)
    assert check_health(path) != 0


def test_freshness_boundary_uses_injected_clock(tmp_path: Path) -> None:
    """The age check is deterministic given ``now``."""
    path = tmp_path / "hb"
    touch_heartbeat(path)
    mtime = path.stat().st_mtime
    assert heartbeat_is_fresh(path, mtime + 10, max_age=10)
    assert not heartbeat_is_fresh(path, mtime + 11, max_age=10)


def test_heartbeat_loop_touches_file_and_stops_when_cancelled(
    tmp_path: Path,
) -> None:
    """AC2/AC3: the loop beats while alive; once it dies, beats stop."""
    path = tmp_path / "hb"

    async def run() -> None:
        task = asyncio.create_task(heartbeat_loop(path, interval=0.01))
        await asyncio.sleep(0.05)
        assert path.is_file()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _set_age(path, HEARTBEAT_MAX_AGE_SECONDS + 5)
        await asyncio.sleep(0.05)
        assert not heartbeat_is_fresh(path, time.time(), HEARTBEAT_MAX_AGE_SECONDS)

    asyncio.run(run())


def test_unreadable_heartbeat_is_unhealthy_not_a_crash(tmp_path: Path) -> None:
    """A non-missing OS error reading the heartbeat means unhealthy, not a traceback."""
    path = tmp_path / "hb"
    path.mkdir()
    blocker = path / "x"
    blocker.write_text("")
    # A path whose parent is a file raises NotADirectoryError (an OSError).
    assert check_health(blocker / "hb") != 0


def test_beat_interval_is_30_seconds() -> None:
    """AC2: heartbeat touched every 30s."""
    assert HEARTBEAT_INTERVAL_SECONDS == 30


def test_healthcheck_fails_within_two_minutes_of_loop_death() -> None:
    """AC3: max heartbeat age plus Docker's check interval is under 2 minutes."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    interval = int(re.search(r"--interval=(\d+)s", dockerfile).group(1))
    retries = int(re.search(r"--retries=(\d+)", dockerfile).group(1))
    timeout = int(re.search(r"--timeout=(\d+)s", dockerfile).group(1))
    worst_case = HEARTBEAT_MAX_AGE_SECONDS + interval * retries + timeout
    assert worst_case <= TWO_MINUTES


def test_dockerfile_healthcheck_runs_the_heartbeat_check() -> None:
    """AC2: the image declares a HEALTHCHECK that calls the checker."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile
    assert "humblebrag.heartbeat" in dockerfile


def test_heartbeat_loop_survives_write_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed heartbeat write is logged and retried, not fatal."""
    path = tmp_path / "missing-dir" / "hb"

    async def run() -> None:
        task = asyncio.create_task(heartbeat_loop(path, interval=0.01))
        await asyncio.sleep(0.05)
        assert not task.done()
        path.parent.mkdir()
        await asyncio.sleep(0.05)
        assert path.is_file()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR):
        asyncio.run(run())
    assert "Failed to write heartbeat file" in caplog.text


def test_healthcheck_main_logs_json_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The healthcheck process logs JSON on stdout like the rest of the app."""

    def unhealthy() -> int:
        heartbeat.logger.error("stale")
        return 1

    monkeypatch.setattr(heartbeat, "check_health", unhealthy)
    with pytest.raises(SystemExit):
        heartbeat.main()
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["level"] == "ERROR"
    assert entry["event"] == "stale"


def test_healthcheck_main_exits_with_check_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2/AC3: the Docker entrypoint exits with the health check's code."""
    monkeypatch.setattr(heartbeat, "check_health", lambda: 1)
    with pytest.raises(SystemExit) as exit_info:
        heartbeat.main()
    assert exit_info.value.code == 1
    monkeypatch.setattr(heartbeat, "check_health", lambda: 0)
    with pytest.raises(SystemExit) as exit_info:
        heartbeat.main()
    assert exit_info.value.code == 0
